"""Pure decision engine — no I/O, ever.

Consumes a `Snapshot` plus an `OperationalContext` (anchor, blind state,
regime target, external liquidation signal) and returns a typed `Action`.

Every function here MUST be pure. If you need a clock, pass it in.
If you need random, pass it in. If you need I/O — you are in the wrong file.

Priority table implemented: README section 7 (P1..P10).
Cushion tranche sizing: README section 8.7 ("25 % du coussin initial").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from delta0.config import Config
from delta0.types import NOOP, Action, Priority, Snapshot, TargetState

# Tolerance for float equality on desired-vs-current exposure comparison.
_EXPOSURE_EPS = 1e-6


class BlindState(StrEnum):
    """Watchdog verdict on venue reachability (README section 11)."""

    NOMINAL = "NOMINAL"
    HL_ONLY = "HL_ONLY"  # only Hyperliquid reachable — reduce short 50% + freeze
    AAVE_ONLY = "AAVE_ONLY"  # only Aave reachable — repay from cushion + freeze
    BOTH_BLIND = "BOTH_BLIND"  # nothing reachable — CRITICAL loop, no action


@dataclass(frozen=True, slots=True)
class OperationalContext:
    """Non-observable state passed alongside a `Snapshot` to `decide()`.

    - `anchor_price`: last recentering reference (None during INIT/BUILD).
    - `now_utc`: current wall clock (UTC), for cron/regime evaluation.
    - `last_skim_at`: timestamp of last successful skim (None if never).
    - `desired_exposure_mult`: regime-gate output, computed by the regime
      evaluator from 30-day funding history + hysteresis. The pure engine
      only compares it to the current exposition.
    - `current_exposure_mult`: currently held exposition (spot / equity).
    - `blind_state`: watchdog verdict.
    - `liquidation_event`: True if the watcher observed a LiquidationCall
      (Aave) or a liquidation user event (HL) for our address.
    """

    now_utc: datetime
    blind_state: BlindState
    liquidation_event: bool = False
    anchor_price: float | None = None
    last_skim_at: datetime | None = None
    desired_exposure_mult: float | None = None
    current_exposure_mult: float | None = None


# --- Target-state solver ------------------------------------------------------


def target_state(equity: float, config: Config) -> TargetState:
    """Solve for the target state given current equity.

    README section 3:
        spot_target     = equity * exposure_mult
        notional_target = spot_target
        margin_target   = spot_target * target_margin_ratio
        debt_target     = target_ltv * spot_target
    """
    if equity <= 0.0:
        raise ValueError(f"equity must be positive, got {equity}")

    spot_target = equity * config.exposure_mult
    notional_target = spot_target
    margin_target = spot_target * config.target_margin_ratio
    debt_target = config.target_ltv * spot_target

    return TargetState(
        spot_target_usd=spot_target,
        notional_target_usd=notional_target,
        margin_target_usd=margin_target,
        debt_target_usd=debt_target,
    )


def cushion_tranche_size(config: Config) -> float:
    """25 % of the *initial* cushion — README section 8.7.

    The initial cushion is `cushion_pct * capital_usd`. We treat the
    reference capital as the tranche denominator; the executor is free
    to clip the tranche to what is actually available.
    """
    initial_cushion = config.cushion_pct * config.capital_usd
    return 0.25 * initial_cushion


# --- Priority evaluators ------------------------------------------------------


def _blind_action(snapshot: Snapshot, config: Config, blind: BlindState) -> Action:
    """BLIND-mode actions (README section 11)."""
    if blind is BlindState.BOTH_BLIND:
        return Action(
            kind="NOOP",
            priority=Priority.P1_LIQUIDATION_DETECTED,
            reason="BLIND: aucune venue joignable — alerte CRITICAL en boucle",
            params={},
        )
    if blind is BlindState.HL_ONLY:
        # Reduce the short to 50 % of current size and freeze.
        target = 0.5 * snapshot.short_size_eth
        return Action(
            kind="REDUCE",
            priority=Priority.P2_EMERGENCY_REDUCE,
            reason="BLIND partiel (HL joignable seul) — réduction 50 % et gel",
            params={
                "close_fraction": 0.5,
                "target_short_size_eth": target,
            },
        )
    # AAVE_ONLY
    tranche = cushion_tranche_size(config)
    return Action(
        kind="REPAY_FROM_CUSHION",
        priority=Priority.P3_EMERGENCY_REPAY,
        reason="BLIND partiel (Aave joignable seul) — remboursement coussin et gel",
        params={"repay_amount_usdc": tranche},
    )


def _p1_liquidation(ctx: OperationalContext, snapshot: Snapshot) -> Action | None:
    if not ctx.liquidation_event:
        return None
    # Match short size to remaining spot after the liquidation.
    target = snapshot.wsteth_atoken_balance  # in ETH terms — assumes wstETH ≈ ETH
    return Action(
        kind="LIQUIDATION_RESPONSE",
        priority=Priority.P1_LIQUIDATION_DETECTED,
        reason="événement de liquidation détecté — couper le short au spot restant",
        params={"target_short_size_eth": target},
    )


def _p2_emergency_reduce(snapshot: Snapshot, config: Config) -> Action | None:
    if snapshot.margin_ratio > config.emergency.margin_ratio_reduce:
        return None
    close_fraction = config.emergency.reduce_fraction
    target = snapshot.short_size_eth * (1.0 - close_fraction)
    return Action(
        kind="REDUCE",
        priority=Priority.P2_EMERGENCY_REDUCE,
        reason=(
            f"marge {snapshot.margin_ratio:.4f} <= seuil réduction "
            f"{config.emergency.margin_ratio_reduce} — IOC {close_fraction:.0%}"
        ),
        params={
            "close_fraction": close_fraction,
            "target_short_size_eth": target,
        },
    )


def _p3_repay_from_cushion(snapshot: Snapshot, config: Config) -> Action | None:
    if snapshot.ltv < config.emergency.ltv_cushion:
        return None
    tranche = cushion_tranche_size(config)
    if snapshot.cushion_usd < tranche:
        return None
    return Action(
        kind="REPAY_FROM_CUSHION",
        priority=Priority.P3_EMERGENCY_REPAY,
        reason=(
            f"LTV {snapshot.ltv:.4f} >= seuil coussin {config.emergency.ltv_cushion} "
            f"— remboursement d'une tranche ({tranche:.0f} USDC)"
        ),
        params={"repay_amount_usdc": tranche},
    )


def _p4_stepwise_deleverage(snapshot: Snapshot, config: Config) -> Action | None:
    if snapshot.ltv < config.emergency.ltv_deleverage:
        return None
    tranche = cushion_tranche_size(config)
    if snapshot.cushion_usd >= tranche:
        # P3 will handle this; P4 is reserved for cushion-exhausted case.
        return None
    return Action(
        kind="STEPWISE_DELEVERAGE",
        priority=Priority.P4_DELEVERAGE,
        reason=(
            f"LTV {snapshot.ltv:.4f} >= seuil désendettement "
            f"{config.emergency.ltv_deleverage} et coussin épuisé "
            f"({snapshot.cushion_usd:.0f} < {tranche:.0f}) — boucle repay/withdraw/swap"
        ),
        params={"target_ltv_after": config.target_ltv + 0.01},
    )


def _p5_pump_up(snapshot: Snapshot, config: Config) -> Action | None:
    if snapshot.margin_ratio > config.emergency.margin_ratio_pump:
        return None
    # Refill the margin to target level.
    target_margin = snapshot.notional_usd * config.target_margin_ratio
    add_amount = max(0.0, target_margin - snapshot.isolated_margin_usd)
    return Action(
        kind="PUMP_UP",
        priority=Priority.P5_PUMP_UP,
        reason=(
            f"marge {snapshot.margin_ratio:.4f} <= seuil pompe "
            f"{config.emergency.margin_ratio_pump} "
            f"— borrow + bridge + add margin ({add_amount:.0f} USDC)"
        ),
        params={"add_margin_amount_usdc": add_amount},
    )


def _p6_pump_down(snapshot: Snapshot, config: Config) -> Action | None:
    if snapshot.ltv < config.emergency.ltv_pump:
        return None
    # Repay enough to bring LTV back to target + 1%.
    target_ltv_after = config.target_ltv + 0.01
    target_debt = target_ltv_after * snapshot.collateral_usd
    repay_amount = max(0.0, snapshot.debt_usd - target_debt)
    return Action(
        kind="PUMP_DOWN",
        priority=Priority.P6_PUMP_DOWN,
        reason=(
            f"LTV {snapshot.ltv:.4f} >= seuil pompe {config.emergency.ltv_pump} "
            f"— withdraw HL + bridge + repay ({repay_amount:.0f} USDC)"
        ),
        params={"repay_amount_usdc": repay_amount},
    )


def _p7_recenter(snapshot: Snapshot, config: Config, ctx: OperationalContext) -> Action | None:
    if ctx.anchor_price is None or ctx.anchor_price <= 0.0:
        return None
    price_move = (snapshot.mark_price - ctx.anchor_price) / ctx.anchor_price
    if price_move >= config.recenter_up:
        return Action(
            kind="RECENTER_UP",
            priority=Priority.P7_RECENTER,
            reason=(
                f"prix +{price_move:.4f} >= seuil re-centrage haut {config.recenter_up} "
                "— re-centrage complet (borrow + bridge + agrandir short)"
            ),
            params={"price_move": price_move},
        )
    if price_move <= -config.recenter_down:
        return Action(
            kind="RECENTER_DOWN",
            priority=Priority.P7_RECENTER,
            reason=(
                f"prix {price_move:.4f} <= seuil re-centrage bas -{config.recenter_down} "
                "— re-centrage complet (withdraw HL + bridge + repay + réduire short)"
            ),
            params={"price_move": price_move},
        )
    return None


def _p8_delta_retrue(snapshot: Snapshot, config: Config) -> Action | None:
    if abs(snapshot.delta_pct) <= config.delta_tolerance:
        return None
    # Bring notional back to spot (delta-neutral).
    # Short size in ETH = spot_usd / mark_price.
    target = snapshot.spot_usd / snapshot.mark_price
    return Action(
        kind="RETRUE_SHORT",
        priority=Priority.P8_DELTA_RETRUE,
        reason=(
            f"delta {snapshot.delta_pct:+.4f} hors tolérance {config.delta_tolerance} "
            "— re-truage du short (maker)"
        ),
        params={"target_short_size_eth": target},
    )


def _p9_skim(snapshot: Snapshot, config: Config, ctx: OperationalContext) -> Action | None:
    # Excess margin above target.
    target_margin = snapshot.notional_usd * config.target_margin_ratio
    excess = snapshot.isolated_margin_usd - target_margin
    if excess < config.skim_min_usd:
        return None
    # Skim schedule check: fire only if the last skim is older than the most recent
    # scheduled slot. The cron string parser lives with the tracer loop; here we
    # accept the caller's `last_skim_at` and treat a None as "never skimmed".
    if _skim_slot_open(ctx.now_utc, config.skim_cron, ctx.last_skim_at):
        return Action(
            kind="SKIM_RECOMPOSE",
            priority=Priority.P9_SKIM,
            reason=(
                f"écrémage: excédent marge {excess:.0f} $ > {config.skim_min_usd:.0f} $ "
                f"et créneau {config.skim_cron} ouvert"
            ),
            params={"excess_margin_usdc": excess},
        )
    return None


def _p10_regime_step(ctx: OperationalContext) -> Action | None:
    if ctx.desired_exposure_mult is None or ctx.current_exposure_mult is None:
        return None
    # Change exposure by 25 % of the gap per tick, per README section 8.9.
    delta = ctx.desired_exposure_mult - ctx.current_exposure_mult
    if abs(delta) < _EXPOSURE_EPS:
        return None
    step_fraction = 0.25
    step_target = ctx.current_exposure_mult + step_fraction * delta
    return Action(
        kind="REGIME_STEP",
        priority=Priority.P10_REGIME,
        reason=(
            f"porte de régime: exposition courante {ctx.current_exposure_mult:.2f}x "
            f"-> cible {ctx.desired_exposure_mult:.2f}x — étape 25 %"
        ),
        params={
            "step_target_exposure_mult": step_target,
            "final_target_exposure_mult": ctx.desired_exposure_mult,
        },
    )


# --- Skim scheduler -----------------------------------------------------------

_DOW_MAP: dict[str, int] = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _skim_slot_open(
    now_utc: datetime,
    cron_spec: str,
    last_skim_at: datetime | None,
) -> bool:
    """Return True if `now_utc` is past the most recent scheduled slot AND
    the previous skim (if any) is older than that slot.

    Only supports the simple "DOW HH:MM UTC" format from the config example.
    """
    try:
        target_dow, hhmm, tz = cron_spec.strip().split()
    except ValueError:
        return False
    if tz.upper() != "UTC":
        return False
    dow_idx = _DOW_MAP.get(target_dow.upper())
    if dow_idx is None:
        return False
    try:
        hh_s, mm_s = hhmm.split(":")
        hh, mm = int(hh_s), int(mm_s)
    except ValueError:
        return False

    # Find the most recent scheduled slot at-or-before now_utc.
    now_dow = now_utc.weekday()
    days_back = (now_dow - dow_idx) % 7
    slot_candidate = now_utc.replace(hour=hh, minute=mm, second=0, microsecond=0)
    slot_candidate = slot_candidate - timedelta(days=days_back)
    if slot_candidate > now_utc:
        slot_candidate = slot_candidate - timedelta(days=7)

    if last_skim_at is None:
        return True
    return bool(last_skim_at < slot_candidate)


# --- Public entry point -------------------------------------------------------


def decide(snapshot: Snapshot, config: Config, ctx: OperationalContext) -> Action:
    """Return the first triggered action according to README section 7.

    BLIND handling is applied first: it overrides normal priorities and only
    lets P1..P4-class actions through.
    """
    # BLIND overrides — P1 (liquidation) still allowed on the reachable venue.
    if ctx.blind_state is not BlindState.NOMINAL:
        if ctx.liquidation_event and ctx.blind_state is not BlindState.AAVE_ONLY:
            hit = _p1_liquidation(ctx, snapshot)
            if hit is not None:
                return hit
        return _blind_action(snapshot, config, ctx.blind_state)

    # NOMINAL: evaluate the full priority table in strict order.
    candidates = (
        _p1_liquidation(ctx, snapshot),
        _p2_emergency_reduce(snapshot, config),
        _p3_repay_from_cushion(snapshot, config),
        _p4_stepwise_deleverage(snapshot, config),
        _p5_pump_up(snapshot, config),
        _p6_pump_down(snapshot, config),
        _p7_recenter(snapshot, config, ctx),
        _p8_delta_retrue(snapshot, config),
        _p9_skim(snapshot, config, ctx),
        _p10_regime_step(ctx),
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return NOOP
