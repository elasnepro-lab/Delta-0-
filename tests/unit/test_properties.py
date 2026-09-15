"""Properties, not examples: what must hold for every state, not five hand-picked ones.

Hypothesis was declared as a dependency from M0 and imported by nothing, while
the rules that matter most are statements about all states: "the highest
triggered priority wins", "P2 never spends more than the reserve", "the solver
is a fixed point". Example tests pin the edges someone thought of; these search
for the ones nobody did (chantier 6.3).

The config is loaded once from `config.yaml.example` rather than through the
pytest fixture: Hypothesis re-runs a test body many times, and a function-scoped
fixture would not be reset between those runs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from delta0.config import Config, load_config
from delta0.decision import (
    BlindState,
    OperationalContext,
    _p1_liquidation,
    _p2_emergency_reduce,
    _p3_repay_from_cushion,
    _p4_stepwise_deleverage,
    _p5_pump_up,
    _p6_pump_down,
    _p7_recenter,
    _p8_delta_retrue,
    _p9_skim,
    _p10_regime_step,
    decide,
    target_state,
)
from delta0.types import Action, Priority, Snapshot
from delta0.units import Rounding, to_raw
from tests.world import REFERENCE_TS, reference_snapshot

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config.yaml.example")

_Evaluator = Callable[[Snapshot, Config, OperationalContext], Action | None]

# The table of README §7, one evaluator per priority, in priority order.
EVALUATORS: tuple[_Evaluator, ...] = (
    lambda s, c, x: _p1_liquidation(x, s),
    lambda s, c, x: _p2_emergency_reduce(s, c),
    lambda s, c, x: _p3_repay_from_cushion(s, c),
    lambda s, c, x: _p4_stepwise_deleverage(s, c),
    lambda s, c, x: _p5_pump_up(s, c),
    lambda s, c, x: _p6_pump_down(s, c),
    _p7_recenter,
    lambda s, c, x: _p8_delta_retrue(s, c),
    _p9_skim,
    lambda s, c, x: _p10_regime_step(x),
)


@st.composite
def positions(draw: st.DrawFn) -> Snapshot:
    """The reference position, pushed anywhere from comfortable to broken."""
    return reference_snapshot(
        hf=draw(st.floats(0.5, 3.0)),
        isolated_margin_usd=draw(st.floats(0.0, 10_000.0)),
        usdc_atoken_balance=draw(st.floats(0.0, 2_000.0)),
        usdc_variable_debt_balance=draw(st.floats(0.0, 45_000.0)),
        short_size_eth=draw(st.floats(0.0, 40.0)),
        wsteth_eth_ratio=draw(st.floats(1.0, 1.4)),
        mark_price=draw(st.floats(500.0, 10_000.0)),
        hl_free_usdc=draw(st.floats(0.0, 5_000.0)),
    )


@st.composite
def contexts(draw: st.DrawFn, blind: BlindState = BlindState.NOMINAL) -> OperationalContext:
    return OperationalContext(
        now_utc=REFERENCE_TS + timedelta(minutes=draw(st.integers(0, 7 * 24 * 60))),
        blind_state=blind,
        liquidation_event=draw(st.booleans()),
        anchor_price=draw(st.none() | st.floats(500.0, 10_000.0)),
        last_skim_at=None,
        desired_exposure_mult=draw(st.none() | st.floats(0.0, 3.0)),
        current_exposure_mult=draw(st.none() | st.floats(0.0, 3.0)),
    )


# --- The decision table -----------------------------------------------------------


@settings(deadline=None)
@given(positions(), contexts())
def test_decide_returns_the_highest_triggered_priority(
    snapshot: Snapshot, ctx: OperationalContext
) -> None:
    fired = [a for evaluate in EVALUATORS if (a := evaluate(snapshot, CONFIG, ctx)) is not None]
    action = decide(snapshot, CONFIG, ctx)
    if not fired:
        assert action.kind == "NOOP"
        return
    assert action.priority == min(a.priority for a in fired)
    assert action in fired


@settings(deadline=None)
@given(
    positions(),
    st.sampled_from([BlindState.HL_ONLY, BlindState.AAVE_ONLY, BlindState.BOTH_BLIND]).flatmap(
        lambda blind: contexts(blind=blind)
    ),
)
def test_a_blind_bot_only_takes_life_safety_actions(
    snapshot: Snapshot, ctx: OperationalContext
) -> None:
    """README §7: priorities 1 to 4 are the only ones allowed while BLIND."""
    assert decide(snapshot, CONFIG, ctx).priority <= Priority.P4_DELEVERAGE


@settings(deadline=None)
@given(positions())
def test_p2_never_spends_more_than_the_reserve_nor_spends_it_in_vain(snapshot: Snapshot) -> None:
    action = _p2_emergency_reduce(snapshot, CONFIG)
    if action is None:
        return
    if action.kind == "ADD_ISOLATED_MARGIN":
        added = float(action.params["add_margin_amount_usdc"])
        assert 0.0 < added <= snapshot.hl_free_usdc + 1e-9
        after = (snapshot.isolated_margin_usd + added) / snapshot.notional_usd
        assert after > CONFIG.emergency.margin_ratio_reduce
    else:
        assert action.kind == "REDUCE"
        target = float(action.params["target_short_size_eth"])
        assert 0.0 <= target < snapshot.short_size_eth


# --- Units ------------------------------------------------------------------------


@given(
    st.floats(0.0, 1e9, allow_nan=False, allow_infinity=False),
    st.integers(0, 18),
)
def test_down_and_up_bracket_the_amount_within_one_unit(amount: float, decimals: int) -> None:
    down = to_raw(amount, decimals, Rounding.DOWN)
    up = to_raw(amount, decimals, Rounding.UP)
    assert 0 <= down <= up <= down + 1


@given(st.decimals(min_value=0, max_value=10**9, places=6, allow_nan=False, allow_infinity=False))
def test_an_amount_on_the_token_grid_converts_exactly(value: Decimal) -> None:
    raw = to_raw(value, 6, Rounding.DOWN)
    assert raw == to_raw(value, 6, Rounding.UP)
    assert Decimal(raw).scaleb(-6) == value


# --- The solver -------------------------------------------------------------------


@settings(deadline=None)
@given(st.floats(1_000.0, 10_000_000.0), st.floats(0.0, 0.2))
def test_the_solver_is_a_fixed_point_for_any_equity(equity: float, cushion_share: float) -> None:
    cushion = equity * cushion_share
    first = target_state(equity=equity, config=CONFIG, cushion_usd=cushion)
    observed = (
        first.spot_target_usd
        + cushion
        + first.margin_target_usd
        + first.reserve_target_usd
        - first.debt_target_usd
    )
    assert observed == pytest.approx(equity, rel=1e-9)
    second = target_state(equity=observed, config=CONFIG, cushion_usd=cushion)
    for name in (
        "spot_target_usd",
        "notional_target_usd",
        "margin_target_usd",
        "reserve_target_usd",
        "debt_target_usd",
    ):
        assert getattr(second, name) == pytest.approx(getattr(first, name), rel=1e-9), name
