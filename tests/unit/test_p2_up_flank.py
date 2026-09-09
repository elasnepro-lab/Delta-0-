"""P2 — the fast defence of the up flank, respecified against measurement.

The README specified a partial close, assuming it pushes the liquidation price
away. It does not: Hyperliquid releases margin in proportion to the size closed,
so the ratio is unchanged. Measured on testnet 2026-09-08 — closing 30 % moved
`liquidationPx` by -0.012 %, while adding 5 USDC of isolated margin to an
86.8 USD notional moved it +5.24 %.

These tests pin the consequence: adding margin is the defence, closing is
damage limitation, and the reserve is therefore not optional.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from delta0.config import Config
from delta0.decision import OperationalContext, decide
from delta0.types import Priority, Snapshot


def _at_margin_ratio(base: Snapshot, ratio: float, *, reserve: float) -> Snapshot:
    return replace(
        base,
        isolated_margin_usd=ratio * base.notional_usd,
        hl_free_usdc=reserve,
    )


def test_the_reserve_is_spent_before_the_short_is_touched(
    stable_snapshot: Snapshot, config: Config, nominal_ctx: OperationalContext
) -> None:
    snap = _at_margin_ratio(stable_snapshot, 0.030, reserve=1_000.0)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P2_EMERGENCY_REDUCE
    assert action.kind == "ADD_ISOLATED_MARGIN"
    assert action.params["add_margin_amount_usdc"] == pytest.approx(1_000.0)


def test_the_add_stops_at_the_nominal_ratio(
    stable_snapshot: Snapshot, config: Config, nominal_ctx: OperationalContext
) -> None:
    """A large reserve tops the margin back up to target, not beyond."""
    snap = _at_margin_ratio(stable_snapshot, 0.030, reserve=50_000.0)
    action = decide(snap, config, nominal_ctx)
    wanted = (config.target_margin_ratio - 0.030) * snap.notional_usd
    assert action.kind == "ADD_ISOLATED_MARGIN"
    assert action.params["add_margin_amount_usdc"] == pytest.approx(wanted)


def test_an_empty_reserve_falls_back_to_closing(
    stable_snapshot: Snapshot, config: Config, nominal_ctx: OperationalContext
) -> None:
    snap = _at_margin_ratio(stable_snapshot, 0.030, reserve=0.0)
    action = decide(snap, config, nominal_ctx)
    assert action.kind == "REDUCE"
    assert action.params["reserve_exhausted"] == 1
    assert "le prix de liquidation ne bouge pas" in action.reason


def test_a_reserve_too_small_to_clear_the_trigger_is_not_spent(
    stable_snapshot: Snapshot, config: Config, nominal_ctx: OperationalContext
) -> None:
    """Spending it would leave the ratio under the trigger and refire next tick.

    Burning the only local defence without leaving the danger zone is strictly
    worse than not spending it: the reserve is gone and the position has not
    moved.
    """
    ratio = 0.030
    snap = _at_margin_ratio(stable_snapshot, ratio, reserve=10.0)
    needed_to_clear = (config.emergency.margin_ratio_reduce - ratio) * snap.notional_usd
    assert needed_to_clear > 10.0  # premise of the test
    action = decide(snap, config, nominal_ctx)
    assert action.kind == "REDUCE"


def test_closing_does_not_restore_the_margin_ratio(
    stable_snapshot: Snapshot,
) -> None:
    """Why REDUCE cannot be the defence, stated as arithmetic.

    Hyperliquid frees margin proportionally, so closing a fraction leaves
    margin/notional exactly where it was — and with it the liquidation price.
    """
    snap = _at_margin_ratio(stable_snapshot, 0.030, reserve=0.0)
    fraction = 0.30
    closed = replace(
        snap,
        short_size_eth=snap.short_size_eth * (1 - fraction),
        isolated_margin_usd=snap.isolated_margin_usd * (1 - fraction),
    )
    assert closed.margin_ratio == pytest.approx(snap.margin_ratio)
    # What it does buy: less exposure at risk if the liquidation happens anyway.
    assert closed.notional_usd < snap.notional_usd


def test_adding_margin_does_restore_the_ratio(stable_snapshot: Snapshot) -> None:
    """The mirror image, and the reason P2 was rewritten around it."""
    snap = _at_margin_ratio(stable_snapshot, 0.030, reserve=1_000.0)
    topped_up = replace(snap, isolated_margin_usd=snap.isolated_margin_usd + 1_000.0)
    assert topped_up.margin_ratio > snap.margin_ratio


def test_p2_stays_silent_above_its_threshold(
    stable_snapshot: Snapshot, config: Config, nominal_ctx: OperationalContext
) -> None:
    snap = _at_margin_ratio(stable_snapshot, 0.0351, reserve=1_000.0)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P2_EMERGENCY_REDUCE


def test_the_reserve_target_is_configured(config: Config) -> None:
    """The classeur has to price it: undeployed capital costs carry."""
    assert 0.0 < config.emergency.hl_reserve_pct < 1.0
    notional = 50_000.0
    reserve = config.emergency.hl_reserve_pct * notional
    # 2 % of notional buys 2 points of margin ratio, which clears the trigger
    # from 0.030 with room to spare.
    assert reserve == pytest.approx(1_000.0)
