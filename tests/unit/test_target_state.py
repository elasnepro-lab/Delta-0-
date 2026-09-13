"""Target-state solver — the reference numbers come from README section 4.

The solver used to be fed raw equity, cushion included, so it asked for a
machine 5 % larger than the balance sheet it was calibrated on. Applied to its
own output it drifted upward every time, which meant the first
skim-and-recompose would have borrowed an extra 1 750 $ and grown the short to
match. The property that closes it is the fixed point pinned below.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

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


def _load_classeur() -> ModuleType:
    """`scripts/` is not a package; load the classeur from its file."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "classeur.py"
    spec = importlib.util.spec_from_file_location("classeur", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: its dataclasses resolve string annotations
    # through `sys.modules`, and a hand-loaded module is not there otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _without_reserve(config: Config) -> Config:
    emergency = config.emergency.model_copy(update={"hl_reserve_pct": 0.0})
    return config.model_copy(update={"emergency": emergency})


def test_reference_balance_matches_the_classeur(config: Config) -> None:
    """Targets must follow the config, not a number frozen in a test.

    The whole point of chantier 1.5: the workbook is generated from the config,
    so the assertions here derive from it too. Hard-coding 50 000 $ is exactly
    how a liquidation threshold of 0.81 survived beside an on-chain 0.79.
    """
    ts = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    mult = config.exposure_mult
    reserve_pct = config.emergency.hl_reserve_pct
    expected_spot = mult * DEPLOYED / (1.0 + mult * reserve_pct)
    assert ts.spot_target_usd == pytest.approx(expected_spot)
    assert ts.notional_target_usd == pytest.approx(expected_spot)
    assert ts.margin_target_usd == pytest.approx(expected_spot * config.target_margin_ratio)
    assert ts.reserve_target_usd == pytest.approx(expected_spot * reserve_pct)
    assert ts.debt_target_usd == pytest.approx(expected_spot * config.target_ltv)


def test_the_solver_and_the_classeur_agree(config: Config) -> None:
    """The classeur is the published balance sheet; the solver is what the bot builds.

    They disagreed by 7.06 % for days — the solver ignored the HL reserve — and
    the classeur printed the gap without anything failing. Checked with and
    without a reserve, so a formula that forgets it cannot pass by accident.
    """
    classeur = _load_classeur()
    # The YAML stores exposure_mult rounded to ten decimals while the classeur
    # recomputes the formula, so ~1e-11 of float noise is expected. The
    # tolerance is the classeur's own: past it, one of the two is wrong.
    tolerance = classeur._MAX_DRIFT
    for cfg in (config, _without_reserve(config)):
        chassis = classeur.solve(cfg, lt=0.79)
        ts = target_state(equity=chassis.capital, config=cfg, cushion_usd=chassis.cushion)
        assert ts.spot_target_usd == pytest.approx(chassis.spot, rel=tolerance)
        assert ts.debt_target_usd == pytest.approx(chassis.debt, rel=tolerance)
        assert ts.margin_target_usd == pytest.approx(chassis.margin, rel=tolerance)
        assert ts.reserve_target_usd == pytest.approx(chassis.reserve, rel=tolerance)


def test_the_reserve_is_not_leveraged(config: Config) -> None:
    """Idle capital cannot also be deployed: the reserve shrinks the machine."""
    assert config.emergency.hl_reserve_pct > 0.0
    with_reserve = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)
    without = target_state(equity=EQUITY, config=_without_reserve(config), cushion_usd=CUSHION)

    assert without.reserve_target_usd == 0.0
    shrink = without.spot_target_usd / with_reserve.spot_target_usd
    assert shrink == pytest.approx(1.0 + config.exposure_mult * config.emergency.hl_reserve_pct)


def test_the_reference_balance_sheet_is_a_fixed_point(config: Config) -> None:
    """Solving for a balance sheet already at target must return that sheet.

    This is the guarantee K10 asked for. Without it the solver is a ratchet:
    every true-up reads the cushion as deployable capital and asks for more
    leverage than the run before.
    """
    first = target_state(equity=EQUITY, config=config, cushion_usd=CUSHION)

    # Rebuild the equity the bot would observe once that target is reached.
    # The reserve sits free on HL, and free balances count in equity.
    observed_equity = (
        first.spot_target_usd
        + CUSHION
        + first.margin_target_usd
        + first.reserve_target_usd
        - first.debt_target_usd
    )
    assert observed_equity == pytest.approx(EQUITY, rel=1e-9)

    second = target_state(equity=observed_equity, config=config, cushion_usd=CUSHION)
    assert second.spot_target_usd == pytest.approx(first.spot_target_usd, rel=1e-9)
    assert second.debt_target_usd == pytest.approx(first.debt_target_usd, rel=1e-9)
    assert second.margin_target_usd == pytest.approx(first.margin_target_usd, rel=1e-9)
    assert second.reserve_target_usd == pytest.approx(first.reserve_target_usd, rel=1e-9)


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
    # reserve_target / notional_target == hl_reserve_pct
    assert ts.reserve_target_usd / ts.notional_target_usd == pytest.approx(
        config.emergency.hl_reserve_pct,
    )


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
    # The softening is exactly the cushion's weight in the collateral:
    #   target - observed = target x cushion / (spot + cushion)
    softening = config.target_ltv * CUSHION / (ts.spot_target_usd + CUSHION)
    assert config.target_ltv - observed_ltv == pytest.approx(softening)
