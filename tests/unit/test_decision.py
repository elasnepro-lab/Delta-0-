"""Exhaustive tests for the decision priority table (README §7 + §15.1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from delta0.config import Config
from delta0.decision import (
    BlindState,
    OperationalContext,
    cushion_tranche_size,
    decide,
    target_state,
)
from delta0.types import Priority, Snapshot

# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def now() -> datetime:
    # Monday 2026-08-24 10:00 UTC.
    return datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def anchor_price() -> float:
    return 2_500.0


@pytest.fixture
def stable_snapshot(now: datetime) -> Snapshot:
    """A snapshot exactly at target: nothing should trigger."""
    return Snapshot(
        ts=now,
        wsteth_atoken_balance=20.0,
        wsteth_price_usd=2_500.0,
        usdc_atoken_balance=1_000.0,
        usdc_variable_debt_balance=35_000.0,
        hf=1.5,
        aave_lt_wsteth=0.83,
        aave_ltv_max_wsteth=0.80,
        aave_emode=0,
        mark_price=2_500.0,
        short_size_eth=20.0,
        isolated_margin_usd=5_000.0,
        hl_maintenance_margin=0.02,
        funding_last_hour=1.25e-5,
        funding_30d_annualized=0.11,
        borrow_apr=0.05,
        gas_eth=0.01,
        ws_last_tick_age_s=1.0,
        rpc_ok=True,
    )


@pytest.fixture
def nominal_ctx(now: datetime, anchor_price: float) -> OperationalContext:
    return OperationalContext(
        now_utc=now,
        blind_state=BlindState.NOMINAL,
        liquidation_event=False,
        anchor_price=anchor_price,
        last_skim_at=None,
        desired_exposure_mult=2.5,
        current_exposure_mult=2.5,
    )


# --- Baseline -----------------------------------------------------------------


def test_stable_state_is_noop(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    action = decide(stable_snapshot, config, nominal_ctx)
    # Monday 10:00 UTC is well before Sunday skim, so no skim either.
    assert action.kind == "NOOP"


# --- P1: liquidation event ---------------------------------------------------


def test_p1_liquidation_event_fires_first(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, liquidation_event=True)
    # Even with concurrent LTV problems, P1 wins.
    snap = replace(
        stable_snapshot,
        usdc_variable_debt_balance=45_000.0,  # LTV would trigger P3/P4
    )
    action = decide(snap, config, ctx)
    assert action.priority is Priority.P1_LIQUIDATION_DETECTED
    assert action.kind == "LIQUIDATION_RESPONSE"
    assert action.params["target_short_size_eth"] == stable_snapshot.wsteth_atoken_balance


# --- P2: emergency reduce (margin_ratio <= 0.035) ----------------------------


def test_p2_edge_just_above_threshold_does_not_fire(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # margin_ratio = 0.0351 — must NOT trigger P2.
    notional = stable_snapshot.notional_usd
    snap = replace(stable_snapshot, isolated_margin_usd=0.0351 * notional)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P2_EMERGENCY_REDUCE


def test_p2_edge_at_threshold_fires(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # margin_ratio = 0.0349 — must trigger P2.
    notional = stable_snapshot.notional_usd
    snap = replace(stable_snapshot, isolated_margin_usd=0.0349 * notional)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P2_EMERGENCY_REDUCE
    assert action.kind == "REDUCE"
    assert action.params["close_fraction"] == pytest.approx(0.30)


# --- P3 vs P4: LTV cushion / deleverage --------------------------------------


def test_p3_edge_below_threshold(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # LTV = 0.789 — must NOT trigger P3.
    snap = _snap_with_ltv(stable_snapshot, 0.789)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P3_EMERGENCY_REPAY


def test_p3_edge_at_threshold(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # LTV = 0.791 — must trigger P3 (cushion has 1000 $ > tranche 250 $).
    snap = _snap_with_ltv(stable_snapshot, 0.791)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P3_EMERGENCY_REPAY
    assert action.params["repay_amount_usdc"] == pytest.approx(cushion_tranche_size(config))


def test_p4_fires_when_ltv_over_deleverage_and_cushion_empty(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # LTV = 0.82, cushion depleted below tranche.
    snap = _snap_with_ltv(stable_snapshot, 0.82)
    snap = replace(snap, usdc_atoken_balance=100.0)  # < tranche 250 $
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P4_DELEVERAGE
    assert action.kind == "STEPWISE_DELEVERAGE"


def test_p3_wins_when_cushion_has_funds_even_at_high_ltv(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.85)
    # Cushion 1000 $ > tranche 250 $: P3 still handles.
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P3_EMERGENCY_REPAY


# --- P5: pump up (margin_ratio <= 0.05) --------------------------------------


def test_p5_fires_below_pump_threshold(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    notional = stable_snapshot.notional_usd
    snap = replace(stable_snapshot, isolated_margin_usd=0.049 * notional)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P5_PUMP_UP
    assert action.kind == "PUMP_UP"


def test_p2_takes_priority_over_p5(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    notional = stable_snapshot.notional_usd
    snap = replace(stable_snapshot, isolated_margin_usd=0.03 * notional)
    # Both P2 (<= 0.035) and P5 (<= 0.05) would fire; P2 wins.
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P2_EMERGENCY_REDUCE


# --- P6: pump down (ltv >= 0.75) ---------------------------------------------


def test_p6_fires_at_ltv_pump(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.76)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P6_PUMP_DOWN
    assert action.kind == "PUMP_DOWN"


def test_p3_takes_priority_over_p6(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.80)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P3_EMERGENCY_REPAY


# --- P7: recenter bands (asymmetric) -----------------------------------------


def test_p7_up_band_edge_below(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # price move +4.4 % — below +4.5 % band.
    snap = replace(stable_snapshot, mark_price=2_500.0 * 1.044)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P7_RECENTER


def test_p7_up_band_edge_above(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # price move +4.6 %.
    snap = replace(stable_snapshot, mark_price=2_500.0 * 1.046, short_size_eth=20.0)
    action = decide(snap, config, nominal_ctx)
    # Delta becomes non-zero here too; P7 (recenter) has higher priority than P8.
    assert action.priority is Priority.P7_RECENTER
    assert action.kind == "RECENTER_UP"


def test_p7_down_band_edge_below(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # price move -5.9 % — inside band (band is -6 %).
    snap = replace(stable_snapshot, mark_price=2_500.0 * 0.941)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P7_RECENTER


def test_p7_down_band_edge_above(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # price move -6.1 %.
    snap = replace(stable_snapshot, mark_price=2_500.0 * 0.939)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P7_RECENTER
    assert action.kind == "RECENTER_DOWN"


def test_p7_no_anchor_yields_no_recenter(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, anchor_price=None)
    snap = replace(stable_snapshot, mark_price=2_500.0 * 1.10)  # +10 %
    action = decide(snap, config, ctx)
    assert action.priority is not Priority.P7_RECENTER


# --- P8: delta retrue (|delta_pct| > 0.02) -----------------------------------


def test_p8_fires_on_small_price_move_below_recenter_band(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # +3 % move: no recenter (band 4.5 %), but delta 3 % > tol 2 %.
    snap = replace(stable_snapshot, mark_price=2_500.0 * 1.03)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P8_DELTA_RETRUE
    assert action.kind == "RETRUE_SHORT"


# --- P9: skim ----------------------------------------------------------------


def test_p9_fires_when_slot_open_and_excess_over_min(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # Sunday 12:30 UTC, never skimmed, big margin excess.
    now_sun = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    ctx = replace(nominal_ctx, now_utc=now_sun, last_skim_at=None)
    snap = replace(stable_snapshot, isolated_margin_usd=6_000.0)  # 1000 $ excess
    action = decide(snap, config, ctx)
    assert action.priority is Priority.P9_SKIM
    assert action.params["excess_margin_usdc"] == pytest.approx(1_000.0)


def test_p9_does_not_fire_below_min(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    now_sun = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    ctx = replace(nominal_ctx, now_utc=now_sun)
    snap = replace(stable_snapshot, isolated_margin_usd=5_100.0)  # 100 $ excess < 200 $
    action = decide(snap, config, ctx)
    assert action.priority is not Priority.P9_SKIM


def test_p9_does_not_fire_if_already_skimmed_this_slot(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    now_sun = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    last_skim = now_sun - timedelta(minutes=5)
    ctx = replace(nominal_ctx, now_utc=now_sun, last_skim_at=last_skim)
    snap = replace(stable_snapshot, isolated_margin_usd=6_000.0)
    action = decide(snap, config, ctx)
    assert action.priority is not Priority.P9_SKIM


# --- P10: regime step --------------------------------------------------------


def test_p10_fires_when_desired_differs(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, current_exposure_mult=2.5, desired_exposure_mult=1.5)
    action = decide(stable_snapshot, config, ctx)
    assert action.priority is Priority.P10_REGIME
    assert action.kind == "REGIME_STEP"
    # 25 % step: 2.5 -> 2.5 + 0.25 * (1.5 - 2.5) = 2.25
    assert action.params["step_target_exposure_mult"] == pytest.approx(2.25)


def test_p10_no_op_when_already_at_target(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, current_exposure_mult=2.5, desired_exposure_mult=2.5)
    action = decide(stable_snapshot, config, ctx)
    assert action.kind == "NOOP"


# --- BLIND handling ----------------------------------------------------------


def test_blind_both_returns_noop_with_critical_reason(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, blind_state=BlindState.BOTH_BLIND)
    action = decide(stable_snapshot, config, ctx)
    assert action.kind == "NOOP"
    assert "BLIND" in action.reason
    assert "CRITICAL" in action.reason


def test_blind_hl_only_reduces_50pct(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, blind_state=BlindState.HL_ONLY)
    action = decide(stable_snapshot, config, ctx)
    assert action.kind == "REDUCE"
    assert action.params["close_fraction"] == pytest.approx(0.5)
    assert action.params["target_short_size_eth"] == pytest.approx(
        0.5 * stable_snapshot.short_size_eth,
    )


def test_blind_aave_only_repays_cushion_tranche(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, blind_state=BlindState.AAVE_ONLY)
    action = decide(stable_snapshot, config, ctx)
    assert action.kind == "REPAY_FROM_CUSHION"
    assert action.params["repay_amount_usdc"] == pytest.approx(cushion_tranche_size(config))


def test_blind_hl_only_still_honours_liquidation_event(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    ctx = replace(nominal_ctx, blind_state=BlindState.HL_ONLY, liquidation_event=True)
    action = decide(stable_snapshot, config, ctx)
    assert action.priority is Priority.P1_LIQUIDATION_DETECTED


# --- Target-state solver (moved from previous test file) ---------------------


def test_target_state_zero_equity_rejected(config: Config) -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        target_state(equity=0.0, config=config)


# --- Helpers -----------------------------------------------------------------


def _snap_with_ltv(base: Snapshot, ltv: float) -> Snapshot:
    """Return a snapshot with the given target LTV (varies debt, keeps collateral)."""
    collateral = base.collateral_usd
    return replace(base, usdc_variable_debt_balance=ltv * collateral)
