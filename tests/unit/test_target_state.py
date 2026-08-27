"""Target-state solver — the reference numbers come from README section 4."""

from __future__ import annotations

import pytest

from delta0.config import Config
from delta0.decision import target_state


def test_reference_balance_model_c(config: Config) -> None:
    """With 20k$ equity and Model C parameters, targets must match the classeur.

    README section 4 assertions (after BUILD, tolerance ±1%):
        wstETH on Aave = 50 000 $
        USDC margin on HL = 5 000 $
        USDC debt = 35 000 $
        Notional short = 50 000 $
    """
    ts = target_state(equity=20_000.0, config=config)
    assert ts.spot_target_usd == pytest.approx(50_000.0, rel=0.01)
    assert ts.notional_target_usd == pytest.approx(50_000.0, rel=0.01)
    assert ts.margin_target_usd == pytest.approx(5_000.0, rel=0.01)
    assert ts.debt_target_usd == pytest.approx(35_000.0, rel=0.01)


def test_solver_scales_linearly(config: Config) -> None:
    ts1 = target_state(equity=10_000.0, config=config)
    ts2 = target_state(equity=20_000.0, config=config)
    assert ts2.spot_target_usd == pytest.approx(2 * ts1.spot_target_usd)
    assert ts2.notional_target_usd == pytest.approx(2 * ts1.notional_target_usd)
    assert ts2.margin_target_usd == pytest.approx(2 * ts1.margin_target_usd)
    assert ts2.debt_target_usd == pytest.approx(2 * ts1.debt_target_usd)


def test_solver_rejects_zero_equity(config: Config) -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        target_state(equity=0.0, config=config)


def test_solver_rejects_negative_equity(config: Config) -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        target_state(equity=-1.0, config=config)


def test_solver_invariants(config: Config) -> None:
    """Cross-checks that the solver preserves the invariants of the solver formula."""
    ts = target_state(equity=20_000.0, config=config)
    # spot == notional (delta-neutral by construction)
    assert ts.spot_target_usd == pytest.approx(ts.notional_target_usd)
    # margin_target / notional_target == target_margin_ratio
    assert ts.margin_target_usd / ts.notional_target_usd == pytest.approx(
        config.target_margin_ratio,
    )
    # debt_target / spot_target == target_ltv (cushion excluded, per solver docstring)
    assert ts.debt_target_usd / ts.spot_target_usd == pytest.approx(config.target_ltv)
