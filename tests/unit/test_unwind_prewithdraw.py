"""Pre-withdraw planning of `scripts/unwind_aave.py`.

The case these tests pin down is the one the M1 marche à blanc produced for
real: a wallet poorer than the debt, so `repay` reverts on the ERC-20 balance
and the collateral is the only place the USDC can come from.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_USDC = 10**6
_LT_USDC_ARB = 7_800  # basis points, as read on-chain


def _module() -> ModuleType:
    """Load the operator script by path — `scripts/` is not a package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "unwind_aave.py"
    spec = importlib.util.spec_from_file_location("unwind_aave", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["unwind_aave"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def unwind() -> ModuleType:
    return _module()


def test_funded_wallet_needs_no_prewithdraw(unwind: ModuleType) -> None:
    amount, refusal = unwind.plan_prewithdraw(
        wallet=50 * _USDC,
        debt=35 * _USDC,
        collateral=175 * _USDC,
        lt_bps=_LT_USDC_ARB,
    )
    assert refusal is None
    assert amount == 0


def test_the_m1_state_withdraws_exactly_the_shortfall(unwind: ModuleType) -> None:
    """The real numbers left by the run: 2,97 free against 35,01 of debt."""
    wallet = 2_973_592
    debt = 35_010_000
    amount, refusal = unwind.plan_prewithdraw(
        wallet=wallet,
        debt=debt,
        collateral=175_020_000,
        lt_bps=_LT_USDC_ARB,
    )
    assert refusal is None
    # Enough to repay the debt AND the buffer the approve already carries.
    assert wallet + amount == debt + unwind._REPAY_BUFFER


def test_a_wallet_one_unit_short_still_plans_a_withdraw(unwind: ModuleType) -> None:
    debt = 35 * _USDC
    amount, refusal = unwind.plan_prewithdraw(
        wallet=debt + unwind._REPAY_BUFFER - 1,
        debt=debt,
        collateral=175 * _USDC,
        lt_bps=_LT_USDC_ARB,
    )
    assert refusal is None
    assert amount == 1


def test_no_collateral_to_draw_on_is_refused(unwind: ModuleType) -> None:
    amount, refusal = unwind.plan_prewithdraw(
        wallet=1 * _USDC,
        debt=35 * _USDC,
        collateral=0,
        lt_bps=_LT_USDC_ARB,
    )
    assert amount == 0
    assert refusal is not None
    assert "aucun collateral" in refusal


def test_a_withdraw_that_would_endanger_the_position_is_refused(
    unwind: ModuleType,
) -> None:
    """Collateral barely above the debt: funding the repay would risk it."""
    amount, refusal = unwind.plan_prewithdraw(
        wallet=0,
        debt=35 * _USDC,
        collateral=40 * _USDC,
        lt_bps=_LT_USDC_ARB,
    )
    assert amount == 0
    assert refusal is not None
    assert "retirables" in refusal


def test_a_zero_liquidation_threshold_is_refused(unwind: ModuleType) -> None:
    amount, refusal = unwind.plan_prewithdraw(
        wallet=0,
        debt=35 * _USDC,
        collateral=175 * _USDC,
        lt_bps=0,
    )
    assert amount == 0
    assert refusal is not None
    assert "seuil de liquidation" in refusal


def test_the_kept_margin_is_never_thinner_than_asked(unwind: ModuleType) -> None:
    debt = 35 * _USDC
    collateral = 175 * _USDC
    amount, refusal = unwind.plan_prewithdraw(
        wallet=0,
        debt=debt,
        collateral=collateral,
        lt_bps=_LT_USDC_ARB,
    )
    assert refusal is None
    hf_after = (collateral - amount) * _LT_USDC_ARB / 10_000 / debt
    assert hf_after >= unwind._MIN_HF_AFTER_PREWITHDRAW
