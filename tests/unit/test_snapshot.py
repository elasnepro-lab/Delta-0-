"""Snapshot derived properties — README section 5."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from delta0.types import Snapshot


def _make_snapshot(**overrides: float | int | bool) -> Snapshot:
    base = {
        "ts": datetime.now(UTC),
        "wsteth_atoken_balance": 16.0,  # 16 wstETH a 3 125 $ = 50 000 $
        "wsteth_price_usd": 3_125.0,
        "wsteth_eth_ratio": 1.25,
        "usdc_atoken_balance": 1_000.0,
        "usdc_variable_debt_balance": 35_000.0,
        "usdc_wallet_balance": 0.0,
        "hf": 1.5,
        "aave_lt_wsteth": 0.83,
        "aave_ltv_max_wsteth": 0.80,
        "aave_emode": 0,
        "mark_price": 2_500.0,
        "short_size_eth": 20.0,
        "isolated_margin_usd": 5_000.0,
        "hl_free_usdc": 1_000.0,
        "hl_maintenance_margin": 0.02,
        "funding_last_hour": 1.25e-5,
        "funding_30d_annualized": 0.11,
        "borrow_apr": 0.05,
        "gas_eth": 0.01,
        "ws_last_tick_age_s": 1.0,
        "rpc_ok": True,
    }
    base.update(overrides)
    return Snapshot(**base)  # type: ignore[arg-type]


def test_spot_notional_delta_zero_at_target() -> None:
    s = _make_snapshot()
    assert s.spot_usd == pytest.approx(50_000.0)
    assert s.notional_usd == pytest.approx(50_000.0)
    assert s.delta_usd == pytest.approx(0.0)
    assert s.delta_pct == pytest.approx(0.0)


def test_ltv_matches_target() -> None:
    s = _make_snapshot()
    # collateral = 50 000 spot + 1 000 cushion = 51 000
    # ltv = 35 000 / 51 000 = 0.6862 (cushion softens LTV — this is intentional)
    assert s.ltv == pytest.approx(35_000.0 / 51_000.0, rel=1e-6)


def test_margin_ratio() -> None:
    s = _make_snapshot()
    assert s.margin_ratio == pytest.approx(0.10)


def test_delta_is_blind_to_price() -> None:
    """Both legs are ETH quantities, so a price move cannot create a delta."""
    s = _make_snapshot(mark_price=2_600.0, wsteth_price_usd=3_250.0)
    assert s.spot_eth_equivalent == pytest.approx(20.0)
    assert s.delta_eth == pytest.approx(0.0)
    assert s.delta_pct == pytest.approx(0.0)


def test_delta_follows_the_staking_rate() -> None:
    """The wstETH/ETH rate drifting up is what actually widens the delta."""
    s = _make_snapshot(wsteth_eth_ratio=1.29)
    assert s.spot_eth_equivalent == pytest.approx(20.64)
    assert s.delta_eth == pytest.approx(0.64)
    assert s.delta_pct == pytest.approx(0.64 / 20.64)


def test_delta_usd_is_reporting_only() -> None:
    """It mixes oracle and mark, so it moves where `delta_eth` does not."""
    s = _make_snapshot(mark_price=2_600.0)
    assert s.delta_usd == pytest.approx(50_000.0 - 52_000.0)
    assert s.delta_eth == pytest.approx(0.0)


def test_carry_spread_positive() -> None:
    s = _make_snapshot()
    assert s.carry_spread == pytest.approx(0.06)


def test_margin_ratio_infinite_when_no_position() -> None:
    s = _make_snapshot(short_size_eth=0.0)
    assert s.margin_ratio == float("inf")


def test_ltv_zero_when_no_collateral() -> None:
    s = _make_snapshot(
        wsteth_atoken_balance=0.0,
        usdc_atoken_balance=0.0,
    )
    assert s.ltv == 0.0


def test_delta_pct_zero_when_no_spot() -> None:
    s = _make_snapshot(wsteth_atoken_balance=0.0)
    assert s.delta_pct == 0.0


def test_equity_reconstruction() -> None:
    s = _make_snapshot()
    # collateral 51 000 + margin 5 000 - debt 35 000 = 21 000
    assert s.equity == pytest.approx(21_000.0)
