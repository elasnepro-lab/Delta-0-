"""Invariants I1-I8, checked on every snapshot — README section 11.

The decision table is the first line of defence; the invariants are the second,
the one that catches what the first let through. The incident of 2026-09-08 is
the case in point: a repay reverted, nothing replayed it, and the Aave leg sat
broken for 60 hours. I2 — never at the cushion threshold for more than five
minutes without a down-flank defence (P3 or P4) in those last five minutes —
would have spoken on the sixth, and kept speaking however many repays had
been emitted and reverted: a defence counts only while it keeps firing.

`check_invariants` is pure, like `decide()`. What it cannot read from a snapshot
(how long a threshold has been crossed, when P2 or P3 last fired, what is in
flight) arrives in an `InvariantContext` that the loop maintains; `breach_since`
and the two `*_breached` predicates keep that bookkeeping trivial and identical
to the triggers `decide()` uses. `assess` turns the violations into the single
verdict the loop acts on, and `emit` raises the alerts.

Escalation (README §11):
- I1, the cruise part of I2 and I3, I4, I6 under an hour, I8: WARN.
- The "never" part of I2 and I3 — the table's defence did not fire in time:
  CRITICAL and deflate, since nothing else is left to wait for.
- I7, or a transfer in transit for over an hour (§9.3): CRITICAL, and
  non-critical operations frozen.
- I5: WARN, and non-critical operations frozen.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from delta0.decision import derive_bands

if TYPE_CHECKING:
    from delta0.config import Config
    from delta0.types import Snapshot


class Severity(StrEnum):
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class TransferInFlight:
    transfer_id: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class InvariantContext:
    """What the invariants need and a snapshot cannot say."""

    now: datetime
    # RUNNING, nothing in flight, no emergency: the steady state I1-I3 describe.
    cruising: bool
    cushion_breach_since: datetime | None = None  # HF at or under the cushion threshold
    # Last down-flank defence emitted, P3 or P4: when the cushion is spent the
    # table answers with P4, and that answer must count.
    last_down_defence_at: datetime | None = None
    margin_breach_since: datetime | None = None  # margin ratio at or under P2's threshold
    # Last up-flank defence emitted, P2 or P1: a liquidation event preempts P2.
    last_up_defence_at: datetime | None = None
    transfers_in_flight: tuple[TransferInFlight, ...] = ()
    executions_in_flight: int = 0
    just_recomposed: bool = False  # a skim-recompose completed since the last check


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    severity: Severity
    message: str


@dataclass(frozen=True, slots=True)
class InvariantVerdict:
    violations: tuple[Violation, ...]
    deflate: bool
    freeze_non_critical: bool

    @property
    def level(self) -> str:
        if not self.violations:
            return "OK"
        if any(v.severity is Severity.CRITICAL for v in self.violations):
            return Severity.CRITICAL.value
        return Severity.WARN.value


# --- Bookkeeping helpers for the loop -------------------------------------------


def breach_since(previous: datetime | None, breached: bool, now: datetime) -> datetime | None:
    """When the current breach started: kept while it lasts, reset once it ends."""
    if not breached:
        return None
    return previous if previous is not None else now


def cushion_breached(snapshot: Snapshot, config: Config) -> bool:
    """P3's own trigger: HF at or under the cushion threshold, on a position with debt."""
    if snapshot.debt_usd <= 0.0 or snapshot.aave_lt_wsteth <= 0.0:
        return False
    return snapshot.hf <= derive_bands(snapshot.aave_lt_wsteth, config).hf_cushion


def margin_breached(snapshot: Snapshot, config: Config) -> bool:
    """P2's own trigger: margin ratio at or under `margin_ratio_reduce`, short open."""
    if snapshot.short_size_eth <= 0.0:
        return False
    return snapshot.margin_ratio <= config.emergency.margin_ratio_reduce


def _unanswered(
    since: datetime | None,
    last_fired: datetime | None,
    now: datetime,
    grace: timedelta,
) -> bool:
    """A breach older than its grace, and no defence fired within that grace.

    A defence emitted once at the start of a breach does not silence the
    invariant for the rest of it: if the repay it sent reverted, the breach
    goes on and so must the alarm. Only a defence inside the last grace window
    — and after the breach began — counts as an answer.
    """
    if since is None or now - since <= grace:
        return False
    return last_fired is None or last_fired < max(since, now - grace)


# --- One generator per invariant --------------------------------------------------


def _i1_delta(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    if ctx.cruising and abs(snapshot.delta_pct) > config.delta_tolerance:
        yield Violation(
            "I1",
            Severity.WARN,
            f"delta {snapshot.delta_pct:+.2%} hors tolérance {config.delta_tolerance:.0%} "
            "en croisière",
        )


def _i2_ltv(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    ceiling = config.target_ltv + config.invariants.cruise_ltv_headroom
    if ctx.cruising and snapshot.debt_usd > 0.0 and snapshot.ltv > ceiling:
        yield Violation(
            "I2", Severity.WARN, f"LTV {snapshot.ltv:.4f} au-dessus de {ceiling:.4f} en croisière"
        )
    grace = timedelta(seconds=config.invariants.p3_grace_s)
    since = ctx.cushion_breach_since
    if (
        since is not None
        and cushion_breached(snapshot, config)
        and _unanswered(since, ctx.last_down_defence_at, ctx.now, grace)
    ):
        held_min = (ctx.now - since).total_seconds() / 60
        yield Violation(
            "I2",
            Severity.CRITICAL,
            f"HF {snapshot.hf:.4f} au seuil du coussin depuis {held_min:.0f} min "
            "sans défense P3 ou P4 récente",
        )


def _i3_margin(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    floor = config.invariants.cruise_margin_floor
    if ctx.cruising and snapshot.short_size_eth > 0.0 and snapshot.margin_ratio < floor:
        yield Violation(
            "I3",
            Severity.WARN,
            f"margin ratio {snapshot.margin_ratio:.4f} sous {floor} en croisière",
        )
    grace = timedelta(seconds=config.invariants.p2_grace_s)
    since = ctx.margin_breach_since
    if (
        since is not None
        and margin_breached(snapshot, config)
        and _unanswered(since, ctx.last_up_defence_at, ctx.now, grace)
    ):
        held_s = (ctx.now - since).total_seconds()
        yield Violation(
            "I3",
            Severity.CRITICAL,
            f"margin ratio {snapshot.margin_ratio:.4f} au seuil de P2 depuis {held_s:.0f} s "
            "sans défense P2 ou P1 récente",
        )


def _i4_cushion(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    _ = ctx
    if snapshot.spot_usd <= 0.0:
        return
    floor = config.cushion_floor_pct * snapshot.equity
    if snapshot.cushion_usd < floor:
        yield Violation(
            "I4",
            Severity.WARN,
            f"coussin {snapshot.cushion_usd:.0f} $ sous le plancher {floor:.0f} $ "
            "— reconstitution prioritaire au prochain écrémage",
        )


def _i5_gas(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    _ = ctx
    if snapshot.gas_eth < config.gas_min_eth:
        yield Violation(
            "I5",
            Severity.WARN,
            f"gaz {snapshot.gas_eth:.4f} ETH sous {config.gas_min_eth} "
            "— opérations non critiques bloquées",
        )


def _i6_transfers(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    _ = snapshot
    rules = config.invariants
    for transfer in ctx.transfers_in_flight:
        age_s = (ctx.now - transfer.started_at).total_seconds()
        if age_s <= rules.transfer_warn_s:
            continue
        severity = Severity.CRITICAL if age_s > rules.transfer_critical_s else Severity.WARN
        yield Violation(
            "I6",
            severity,
            f"transfert {transfer.transfer_id} en transit depuis {age_s / 60:.0f} min",
        )


def _i7_one_execution(
    snapshot: Snapshot, config: Config, ctx: InvariantContext
) -> Iterator[Violation]:
    _ = snapshot, config
    if ctx.executions_in_flight > 1:
        yield Violation(
            "I7",
            Severity.CRITICAL,
            f"{ctx.executions_in_flight} opérations d'exécution en cours simultanément",
        )


def _i8_recompose(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> Iterator[Violation]:
    # The LTV compared is the solver's (debt / spot): Aave's own reads lower, the
    # cushion counting as collateral.
    if not ctx.just_recomposed or snapshot.spot_usd <= 0.0 or snapshot.notional_usd <= 0.0:
        return
    debt_ratio = snapshot.debt_usd / snapshot.spot_usd
    tolerance = config.invariants.recompose_tolerance
    ltv_off = abs(debt_ratio - config.target_ltv) > tolerance
    margin_off = abs(snapshot.margin_ratio - config.target_margin_ratio) > tolerance
    if ltv_off or margin_off:
        yield Violation(
            "I8",
            Severity.WARN,
            f"après recomposition : dette/spot {debt_ratio:.4f} (cible {config.target_ltv}), "
            f"marge {snapshot.margin_ratio:.4f} (cible {config.target_margin_ratio})",
        )


_CHECKS = (
    _i1_delta,
    _i2_ltv,
    _i3_margin,
    _i4_cushion,
    _i5_gas,
    _i6_transfers,
    _i7_one_execution,
    _i8_recompose,
)


def check_invariants(snapshot: Snapshot, config: Config, ctx: InvariantContext) -> list[Violation]:
    """Every violated invariant, in I1-I8 order. Pure."""
    return [violation for check in _CHECKS for violation in check(snapshot, config, ctx)]


# --- Verdict and alerts ---------------------------------------------------------

# A defence that should have fired and did not leaves nothing else to wait for.
_DEFLATE_ON = frozenset({"I2", "I3"})


def assess(violations: list[Violation]) -> InvariantVerdict:
    """The one verdict the loop acts on."""
    critical = [v for v in violations if v.severity is Severity.CRITICAL]
    return InvariantVerdict(
        violations=tuple(violations),
        deflate=any(v.code in _DEFLATE_ON for v in critical),
        freeze_non_critical=bool(critical) or any(v.code == "I5" for v in violations),
    )


class _Log(Protocol):
    def warning(self, event: str, **kw: Any) -> Any: ...
    def critical(self, event: str, **kw: Any) -> Any: ...


def emit(verdict: InvariantVerdict, log: _Log) -> None:
    """One log event per violation, which the alert processor turns into a message.

    Each invariant carries its own event name: the alert sink collapses repeats
    of the same event for fifteen minutes, and one shared name would let a
    running I2 hide a brand-new I7 behind it.
    """
    for violation in verdict.violations:
        event = f"invariant_{violation.code.lower()}_violated"
        message = f"invariant {violation.code} violé : {violation.message}"
        if violation.severity is Severity.CRITICAL:
            log.critical(event, message=message, code=violation.code)
        else:
            log.warning(event, message=message, code=violation.code)
