"""Target-state solver — the reference numbers come from README section 4.

The solver used to be fed raw equity, cushion included, so it asked for a
machine 5 % larger than the balance sheet it was calibrated on. Applied to its
own output it drifted upward every time, which meant the first
skim-and-recompose would have borrowed an extra 1 750 $ and grown the short to
match. The property that closes it is the fixed point pinned below.
"""

from __future__ import annotations

import pytest

from delta0.config import Config
from delta0.decision import target_state

# Reference chassis, README §4 and the Model C workbook: 20 000 $ deployed
# alongside a 1 000 $ cushion, so 21 000 $ of equity in total. README §4 says
# "capital 20 000" without saying which side of the cushion it falls on — the
# workbook's own numbers only reconcile with this reading, and making the text
# say so is part of the classeur pass.
DEPLOYED = 20_000.0
CUSHION = 1_000.0
EQUITY = DEPLOYED + CUSHION


def test_reference_balance_model_c(config: Config) -> None:
    """With Model C parameters, targets must match the classeur (±1 %)."""
    ts = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    assert ts.spot_target_usd == pytest.approx(50_000.0, rel=0.01)
    assert ts.notional_target_usd == pytest.approx(50_000.0, rel=0.01)
    assert ts.margin_target_usd == pytest.approx(5_000.0, rel=0.01)
    assert ts.debt_target_usd == pytest.approx(35_000.0, rel=0.01)


def test_the_reference_balance_sheet_is_a_fixed_point(config: Config) -> None:
    """Solving for a balance sheet already at target must return that sheet.

    This is the guarantee K10 asked for. Without it the solver is a ratchet:
    every true-up reads the cushion as deployable capital and asks for more
    leverage than the run before.
    """
    first = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)

    # Rebuild the equity the bot would observe once that target is reached.
    observed_equity = (
        first.spot_target_usd + CUSHION + first.margin_target_usd - first.debt_target_usd
    )
    assert observed_equity == pytest.approx(EQUITY, rel=1e-9)

    second = target_state(equity=observed_equity, config=config, cushion_usd=CUSHION)
    assert second.spot_target_usd == pytest.approx(first.spot_target_usd, rel=1e-9)
    assert second.debt_target_usd == pytest.approx(first.debt_target_usd, rel=1e-9)
    assert second.margin_target_usd == pytest.approx(first.margin_target_usd, rel=1e-9)


def test_the_cushion_is_not_leveraged(config: Config) -> None:
    """Counting the cushion as deployable inflates the whole machine."""
    correct = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    inflated = target_state(equity=EQUITY, config=config, cushion_usd=0.0)
    overshoot = inflated.spot_target_usd / correct.spot_target_usd - 1
    assert overshoot == pytest.approx(CUSHION / DEPLOYED, rel=1e-6)  # +5 %
    assert inflated.debt_target_usd > correct.debt_target_usd


def test_solver_scales_linearly(config: Config) -> None:
    ts1 = target_state(equity=10_500.0, config=config, cushion_usd=500.0)
    ts2 = target_state(equity=21_000.0, config=config, cushion_usd=1_000.0)
    assert ts2.spot_target_usd == pytest.approx(2 * ts1.spot_target_usd)
    assert ts2.notional_target_usd == pytest.approx(2 * ts1.notional_target_usd)
    assert ts2.margin_target_usd == pytest.approx(2 * ts1.margin_target_usd)
    assert ts2.debt_target_usd == pytest.approx(2 * ts1.debt_target_usd)


def test_solver_rejects_zero_equity(config: Config) -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        target_state(equity=0.0, config=config, cushion_usd=0.0)


def test_solver_rejects_negative_equity(config: Config) -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        target_state(equity=-1.0, config=config, cushion_usd=0.0)


def test_solver_rejects_a_cushion_that_swallows_the_equity(config: Config) -> None:
    """Nothing left to deploy is a configuration error, not a tiny position."""
    with pytest.raises(ValueError, match="no deployable equity"):
        target_state(equity=1_000.0, config=config, cushion_usd=1_000.0)


def test_solver_rejects_a_negative_cushion(config: Config) -> None:
    with pytest.raises(ValueError, match="cushion must not be negative"):
        target_state(equity=20_000.0, config=config, cushion_usd=-1.0)


def test_solver_invariants(config: Config) -> None:
    """Cross-checks that the solver preserves the invariants of its formula."""
    ts = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    # spot == notional (delta-neutral by construction)
    assert ts.spot_target_usd == pytest.approx(ts.notional_target_usd)
    # margin_target / notional_target == target_margin_ratio
    assert ts.margin_target_usd / ts.notional_target_usd == pytest.approx(
        config.target_margin_ratio,
    )
    # debt_target / spot_target == target_ltv (cushion excluded, per docstring)
    assert ts.debt_target_usd / ts.spot_target_usd == pytest.approx(config.target_ltv)


def test_the_resulting_ltv_sits_below_the_nominal_target(config: Config) -> None:
    """Aave counts the cushion as collateral, so the observed LTV is softer.

    Deliberate: sizing the debt on spot + cushion would let the cushion carry
    its own debt, which costs more band than it buys once spent
    (memory/aave_findings.md §11). The gap belongs in the digest so nobody
    reads it as drift.
    """
    ts = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    observed_ltv = ts.debt_target_usd / (ts.spot_target_usd + CUSHION)
    assert observed_ltv < config.target_ltv
    assert observed_ltv == pytest.approx(35_000.0 / 51_000.0, rel=1e-6)
