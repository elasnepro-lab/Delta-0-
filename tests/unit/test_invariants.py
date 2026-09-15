"""Invariants I1-I8 — the second line of defence, README section 11.

Each invariant is exercised on the reference world of `conftest.py` (16 wstETH
against a 20 ETH short, at rest), moved just enough to break that one rule.
The "never" halves of I2 and I3 get the most attention: they are what would
have caught the 60-hour silence of 2026-09-08.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from delta0.config import Config, load_config
from delta0.config.schema import InvariantsConfig
from delta0.invariants import (
    InvariantContext,
    Severity,
    TransferInFlight,
    Violation,
    assess,
    breach_since,
    check_invariants,
    cushion_breached,
    emit,
    margin_breached,
)
from delta0.types import Snapshot


def _at_rest(now: datetime, **overrides: Any) -> InvariantContext:
    return InvariantContext(now=now, cruising=True, **overrides)


def _codes(violations: list[Violation]) -> list[tuple[str, Severity]]:
    return sorted((v.code, v.severity) for v in violations)


def test_a_balanced_position_at_rest_violates_nothing(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    violations = check_invariants(stable_snapshot, config, _at_rest(now))
    assert violations == []
    verdict = assess(violations)
    assert verdict.level == "OK"
    assert not verdict.deflate
    assert not verdict.freeze_non_critical


# --- I1 -------------------------------------------------------------------------


def test_i1_warns_on_delta_only_at_rest(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    unhedged = replace(stable_snapshot, short_size_eth=19.0)  # 20 ETH of spot, 5 % bare
    assert _codes(check_invariants(unhedged, config, _at_rest(now))) == [("I1", Severity.WARN)]
    # Mid-operation a transient delta is expected, not a violation.
    moving = InvariantContext(now=now, cruising=False)
    assert check_invariants(unhedged, config, moving) == []


# --- I2 -------------------------------------------------------------------------


def test_i2_warns_when_ltv_leaves_its_cruise_headroom(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    heavier = replace(stable_snapshot, usdc_variable_debt_balance=36_000.0)  # LTV 0.706
    assert _codes(check_invariants(heavier, config, _at_rest(now))) == [("I2", Severity.WARN)]


def test_i2_turns_critical_and_deflates_when_p3_never_answers(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    """The check that would have spoken on the sixth minute of 2026-09-08."""
    breached = replace(stable_snapshot, hf=1.02)  # under LT / (LT - cushion margin) = 1.0327
    assert cushion_breached(breached, config)
    since = now - timedelta(minutes=6)

    silent = InvariantContext(now=now, cruising=False, cushion_breach_since=since)
    violations = check_invariants(breached, config, silent)
    assert _codes(violations) == [("I2", Severity.CRITICAL)]
    verdict = assess(violations)
    assert verdict.level == "CRITICAL"
    assert verdict.deflate
    assert verdict.freeze_non_critical


def test_i2_holds_while_p3_answered_or_within_grace(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    breached = replace(stable_snapshot, hf=1.02)
    since = now - timedelta(minutes=6)

    answered = InvariantContext(
        now=now,
        cruising=False,
        cushion_breach_since=since,
        last_down_defence_at=now - timedelta(minutes=2),
    )
    assert check_invariants(breached, config, answered) == []

    fresh = InvariantContext(
        now=now, cruising=False, cushion_breach_since=now - timedelta(minutes=4)
    )
    assert check_invariants(breached, config, fresh) == []


def test_i2_a_p3_from_an_earlier_breach_does_not_count(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    breached = replace(stable_snapshot, hf=1.02)
    since = now - timedelta(minutes=6)
    stale = InvariantContext(
        now=now,
        cruising=False,
        cushion_breach_since=since,
        last_down_defence_at=since - timedelta(hours=2),
    )
    assert _codes(check_invariants(breached, config, stale)) == [("I2", Severity.CRITICAL)]


def test_i2_a_defence_fired_once_does_not_silence_a_breach_that_goes_on(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    """The 2026-09-08 shape: a repay emitted, reverted, and the breach still there.

    A first version counted any defence since the breach began, so a single P3
    at minute one silenced I2 for hours. Only a defence within the last grace
    window answers the breach.
    """
    breached = replace(stable_snapshot, hf=1.02)
    since = now - timedelta(minutes=20)
    once = InvariantContext(
        now=now,
        cruising=False,
        cushion_breach_since=since,
        last_down_defence_at=since + timedelta(minutes=1),
    )
    assert _codes(check_invariants(breached, config, once)) == [("I2", Severity.CRITICAL)]


def test_a_position_without_debt_cannot_breach_the_cushion(
    stable_snapshot: Snapshot, config: Config
) -> None:
    """An empty Aave account reads LT 0 and HF infinite: no breach to invent."""
    empty = replace(stable_snapshot, usdc_variable_debt_balance=0.0, aave_lt_wsteth=0.0, hf=0.0)
    assert not cushion_breached(empty, config)


# --- I3 -------------------------------------------------------------------------


def test_i3_warns_when_margin_dips_under_its_cruise_floor(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    thinner = replace(stable_snapshot, isolated_margin_usd=3_000.0)  # 0.06 of 50 000
    assert _codes(check_invariants(thinner, config, _at_rest(now))) == [("I3", Severity.WARN)]


def test_i3_turns_critical_and_deflates_when_p2_never_answers(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    breached = replace(stable_snapshot, isolated_margin_usd=1_500.0)  # 0.03, under 0.035
    assert margin_breached(breached, config)
    since = now - timedelta(seconds=11)

    silent = InvariantContext(now=now, cruising=False, margin_breach_since=since)
    violations = check_invariants(breached, config, silent)
    assert _codes(violations) == [("I3", Severity.CRITICAL)]
    assert assess(violations).deflate

    answered = InvariantContext(
        now=now,
        cruising=False,
        margin_breach_since=since,
        last_up_defence_at=now - timedelta(seconds=5),
    )
    assert check_invariants(breached, config, answered) == []


# --- I4, I5 ---------------------------------------------------------------------


def test_i4_warns_when_the_cushion_sinks_under_its_floor(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    spent = replace(stable_snapshot, usdc_atoken_balance=400.0)  # floor 2.5 % of 21 400 = 535
    moving = InvariantContext(now=now, cruising=False)
    assert _codes(check_invariants(spent, config, moving)) == [("I4", Severity.WARN)]


def test_i5_warns_and_freezes_non_critical_operations(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    dry = replace(stable_snapshot, gas_eth=0.001)
    violations = check_invariants(dry, config, _at_rest(now))
    assert _codes(violations) == [("I5", Severity.WARN)]
    verdict = assess(violations)
    assert verdict.freeze_non_critical
    assert not verdict.deflate


# --- I6, I7 ---------------------------------------------------------------------


def test_i6_escalates_with_the_age_of_a_transfer(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    ctx = _at_rest(
        now,
        transfers_in_flight=(
            TransferInFlight("aller", now - timedelta(minutes=5)),
            TransferInFlight("retour", now - timedelta(minutes=20)),
            TransferInFlight("perdu", now - timedelta(minutes=70)),
        ),
    )
    violations = check_invariants(stable_snapshot, config, ctx)
    assert _codes(violations) == [("I6", Severity.CRITICAL), ("I6", Severity.WARN)]
    verdict = assess(violations)
    assert verdict.freeze_non_critical
    assert not verdict.deflate  # a lost transfer freezes; it does not unwind the machine


def test_i7_two_executions_at_once_is_critical_without_deflating(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    assert check_invariants(stable_snapshot, config, _at_rest(now, executions_in_flight=1)) == []
    violations = check_invariants(stable_snapshot, config, _at_rest(now, executions_in_flight=2))
    assert _codes(violations) == [("I7", Severity.CRITICAL)]
    verdict = assess(violations)
    assert verdict.freeze_non_critical
    assert not verdict.deflate


# --- I8 -------------------------------------------------------------------------


def test_i8_checks_the_targets_after_a_recompose(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    # The reference world carries debt/spot 0.70 against a 0.675 target.
    off_target = _at_rest(now, just_recomposed=True)
    assert _codes(check_invariants(stable_snapshot, config, off_target)) == [("I8", Severity.WARN)]

    on_target = replace(
        stable_snapshot, usdc_variable_debt_balance=config.target_ltv * stable_snapshot.spot_usd
    )
    assert check_invariants(on_target, config, off_target) == []
    # Without a recompose, the same gap is none of I8's business.
    assert check_invariants(stable_snapshot, config, _at_rest(now)) == []


# --- Bookkeeping, alerts, config --------------------------------------------------


def test_breach_since_keeps_the_start_and_resets_on_recovery(now: datetime) -> None:
    earlier = now - timedelta(minutes=3)
    assert breach_since(None, breached=True, now=now) == now
    assert breach_since(earlier, breached=True, now=now) == earlier
    assert breach_since(earlier, breached=False, now=now) is None


def test_each_invariant_raises_its_own_alert_event(
    stable_snapshot: Snapshot, config: Config, now: datetime
) -> None:
    """One shared event name would let a running I2 hide a new I7 for 15 minutes."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _Recorder:
        def warning(self, event: str, **kw: Any) -> None:
            calls.append(("warning", event, kw))

        def critical(self, event: str, **kw: Any) -> None:
            calls.append(("critical", event, kw))

    broken = replace(stable_snapshot, hf=1.02, gas_eth=0.001)
    ctx = InvariantContext(
        now=now,
        cruising=False,
        cushion_breach_since=now - timedelta(minutes=6),
        executions_in_flight=2,
    )
    emit(assess(check_invariants(broken, config, ctx)), _Recorder())

    events = {(level, event) for level, event, _ in calls}
    assert events == {
        ("critical", "invariant_i2_violated"),
        ("warning", "invariant_i5_violated"),
        ("critical", "invariant_i7_violated"),
    }
    assert all(kw["message"].startswith("invariant I") for _, _, kw in calls)


def test_invariant_defaults_are_the_readme_numbers(config: Config) -> None:
    rules = config.invariants
    assert rules.cruise_ltv_headroom == pytest.approx(0.02)
    assert rules.cruise_margin_floor == pytest.approx(0.07)
    assert rules.p3_grace_s == pytest.approx(300.0)
    assert rules.transfer_warn_s == pytest.approx(900.0)
    assert rules.transfer_critical_s == pytest.approx(3_600.0)
    assert rules.recompose_tolerance == pytest.approx(0.005)
    assert InvariantsConfig() == rules


def test_transfer_thresholds_must_escalate() -> None:
    with pytest.raises(ValidationError, match="transfer_critical_s must be strictly greater"):
        InvariantsConfig(transfer_warn_s=3_600.0, transfer_critical_s=900.0)


def test_cruise_margin_floor_must_sit_between_pump_and_target(
    example_config_path: Path, tmp_path: Path
) -> None:
    raw = yaml.safe_load(example_config_path.read_text(encoding="utf-8"))
    raw["invariants"]["cruise_margin_floor"] = 0.04  # under the 0.05 pump
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="cruise_margin_floor"):
        load_config(bad)
