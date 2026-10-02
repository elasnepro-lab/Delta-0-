"""Exhaustive tests for the decision priority table (README §7 + §15.1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from delta0.config import Config
from delta0.decision import (
    BlindState,
    OperationalContext,
    Regime,
    cushion_tranche_size,
    decide,
    exposure_mult_of,
    regime_candidate,
    regime_step,
    target_state,
)
from delta0.types import Priority, Snapshot

# --- Fixtures -----------------------------------------------------------------


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
    # The target is the collateral in ETH terms, not the raw wstETH balance:
    # 16 wstETH at 1.25 ETH each is 20 ETH to hedge, not 16.
    assert action.params["target_short_size_eth"] == pytest.approx(20.0)
    assert action.params["target_short_size_eth"] == stable_snapshot.spot_eth_equivalent


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
    # margin_ratio = 0.0349 — must trigger P2. With a reserve on hand the
    # defence is to add margin, not to close: see test_p2_up_flank.
    notional = stable_snapshot.notional_usd
    snap = replace(stable_snapshot, isolated_margin_usd=0.0349 * notional)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P2_EMERGENCY_REDUCE
    assert action.kind == "ADD_ISOLATED_MARGIN"


# --- P3 vs P4 : bandes derivees du LT on-chain -------------------------------


def test_p3_edge_below_threshold(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # LTV 0.764, juste sous le coussin derive (0.765) — P3 ne doit pas tirer.
    snap = _snap_with_ltv(stable_snapshot, 0.764)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P3_EMERGENCY_REPAY


def test_p3_edge_at_threshold(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # LTV 0.766, juste au-dessus du coussin — P3 tire (coussin 1000 $ > tranche 250 $).
    snap = _snap_with_ltv(stable_snapshot, 0.766)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P3_EMERGENCY_REPAY
    assert action.params["repay_amount_usdc"] == pytest.approx(cushion_tranche_size(config))


def test_p4_fires_when_ltv_over_deleverage_and_cushion_empty(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    # Coussin vide d'abord : il entre dans le collatéral, donc dans le HF.
    depleted = replace(stable_snapshot, usdc_atoken_balance=100.0)  # < tranche 250 $
    snap = _snap_with_ltv(depleted, 0.78)  # au-delà du désendettement (0.775)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P4_DELEVERAGE
    assert action.kind == "STEPWISE_DELEVERAGE"


def test_p3_wins_when_cushion_has_funds_even_at_high_ltv(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.785)
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


# --- P6: pump down (LT 0.79 - marge 0.040 = 0.75) ---------------------------------------------


def test_p6_fires_at_ltv_pump(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.755)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P6_PUMP_DOWN
    assert action.kind == "PUMP_DOWN"


def test_p3_takes_priority_over_p6(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    snap = _snap_with_ltv(stable_snapshot, 0.77)
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


def test_p8_fires_when_the_hedged_quantity_drifts(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    """Staking accrual raises the wstETH/ETH rate, so the long leg grows."""
    # 16 wstETH at 1.29 ETH = 20.64 ETH against a 20 ETH short: delta 3.1 %.
    snap = replace(stable_snapshot, wsteth_eth_ratio=1.29)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P8_DELTA_RETRUE
    assert action.kind == "RETRUE_SHORT"
    assert action.params["target_short_size_eth"] == pytest.approx(20.64)


def test_p8_ignores_a_pure_price_move(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    """Neutrality is an equality of ETH quantities, so price cannot break it.

    Both legs are denominated in ETH: the collateral through the oracle rate,
    the short by construction. A move that leaves the quantities alone leaves
    the delta alone — and must not spend fees re-hedging noise.
    """
    snap = replace(stable_snapshot, mark_price=2_500.0 * 1.03, wsteth_price_usd=3_125.0 * 1.03)
    assert snap.delta_pct == pytest.approx(0.0)
    action = decide(snap, config, nominal_ctx)
    assert action.priority is not Priority.P8_DELTA_RETRUE


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
        target_state(equity=0.0, config=config, cushion_usd=0.0)


# --- Helpers -----------------------------------------------------------------


def _snap_with_ltv(base: Snapshot, ltv: float) -> Snapshot:
    """Return a snapshot at the given LTV, health factor included.

    The health factor has to move with the debt: it is what P3, P4 and P6 read,
    and a fixture that raised the debt while leaving `hf` frozen described a
    position Aave could never report. That decoupling is exactly what let the
    old thresholds look tested.
    """
    collateral = base.collateral_usd
    debt = ltv * collateral
    hf = float("inf") if debt == 0 else base.aave_lt_wsteth * collateral / debt
    return replace(base, usdc_variable_debt_balance=debt, hf=hf)


# --- La porte de régime : l'évaluateur, enfin écrit ----------------------------


def test_exposure_mult_of_inverse_exactement_le_solveur(config: Config) -> None:
    """Les deux expositions doivent se mesurer pareil, sinon P10 vise un point
    qu'il ne peut jamais rapporter avoir atteint."""
    cushion = config.capital_usd * config.cushion_pct
    target = target_state(config.capital_usd, config, cushion_usd=cushion)
    held = exposure_mult_of(target.spot_target_usd, config.capital_usd, cushion, config)
    assert held == pytest.approx(config.exposure_mult)


def test_exposure_mult_of_rend_zero_sur_un_bilan_a_plat(config: Config) -> None:
    assert exposure_mult_of(0.0, 20_000.0, 1_000.0, config) == 0.0


def test_target_state_sait_dimensionner_a_une_autre_exposition(config: Config) -> None:
    """Sans ce paramètre, P10 émettait une exposition que rien ne savait poser."""
    cushion = 1_000.0
    plein = target_state(20_000.0, config, cushion_usd=cushion)
    moitie = target_state(
        20_000.0, config, cushion_usd=cushion, exposure_mult=config.exposure_mult_half
    )
    assert moitie.spot_target_usd < plein.spot_target_usd
    assert moitie.debt_target_usd == pytest.approx(config.target_ltv * moitie.spot_target_usd)


def test_une_exposition_nulle_donne_un_bilan_a_plat(config: Config) -> None:
    """PARKED n'est pas un cas particulier : c'est la cible zéro, résolue comme les autres."""
    plat = target_state(20_000.0, config, cushion_usd=1_000.0, exposure_mult=0.0)
    assert plat.spot_target_usd == 0.0
    assert plat.debt_target_usd == 0.0
    assert plat.reserve_target_usd == 0.0


def test_une_exposition_negative_est_refusee(config: Config) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        target_state(20_000.0, config, cushion_usd=1_000.0, exposure_mult=-1.0)


@pytest.mark.parametrize(
    ("spread", "attendu"),
    [
        (0.08, "plein"),  # 800 bps, au-dessus du seuil de 500
        (0.05, "plein"),  # pile sur le seuil : la borne est incluse
        (0.02, "moitie"),
        (0.0, "moitie"),  # carry nul mais pas négatif
        (-0.01, "zero"),
    ],
)
def test_le_regime_lit_le_spread_par_bandes(config: Config, spread: float, attendu: str) -> None:
    attendus = {
        "plein": config.exposure_mult,
        "moitie": config.exposure_mult_half,
        "zero": 0.0,
    }
    assert regime_candidate(spread, config) == attendus[attendu]


def test_la_porte_ne_bouge_pas_avant_la_confirmation(config: Config) -> None:
    """Le funding traverse zéro sans arrêt ; une porte sans mémoire re-bâtirait
    tout le montage sur un mauvais après-midi."""
    state = Regime(target=config.exposure_mult, candidate=config.exposure_mult, days=30)
    for jour in range(1, config.regime.hysteresis_days):
        state = regime_step(state, -0.01, config)
        assert state.target == config.exposure_mult, f"jour {jour} : trop tôt"
        assert state.candidate == 0.0
    state = regime_step(state, -0.01, config)
    assert state.target == 0.0
    assert state.parked


def test_un_regime_qui_change_d_avis_repart_de_un(config: Config) -> None:
    state = Regime(target=config.exposure_mult, candidate=config.exposure_mult, days=30)
    for _ in range(config.regime.hysteresis_days - 1):
        state = regime_step(state, -0.01, config)
    state = regime_step(state, 0.08, config)  # le carry redevient large
    assert state.days == 1
    assert state.target == config.exposure_mult, "rien n'a jamais été confirmé"


def test_un_regime_confirme_qui_dure_ne_re_declenche_rien(config: Config) -> None:
    state = Regime(target=config.exposure_mult, candidate=config.exposure_mult, days=30)
    for _ in range(20):
        state = regime_step(state, 0.08, config)
    assert state.target == config.exposure_mult
    assert state.days == 50
