"""Boot reconciliation — drift detection between journal and on-chain."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from delta0.reconcile import reconcile_at_boot
from delta0.state import StateStore
from delta0.types import Snapshot


def _snap(**overrides: float | int | bool) -> Snapshot:
    base: dict[str, object] = {
        "ts": datetime.now(UTC),
        "wsteth_atoken_balance": 20.0,
        "wsteth_price_usd": 2_500.0,
        "usdc_atoken_balance": 1_000.0,
        "usdc_variable_debt_balance": 35_000.0,
        "hf": 1.5,
        "aave_lt_wsteth": 0.83,
        "aave_ltv_max_wsteth": 0.80,
        "aave_emode": 0,
        "mark_price": 2_500.0,
        "short_size_eth": 20.0,
        "isolated_margin_usd": 5_000.0,
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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_first_boot_no_anchor(store: StateStore) -> None:
    report = await reconcile_at_boot(store, _snap())
    assert report.anchor_journal is None
    assert report.anchor_drift_pct is None
    assert report.warnings == ()


@pytest.mark.asyncio
async def test_small_anchor_drift_no_warning(store: StateStore) -> None:
    await store.kv_set("anchor_price", "2500.0")
    snap = _snap(mark_price=2_500.0 * 1.05)  # +5 %, well under 15 %
    report = await reconcile_at_boot(store, snap)
    assert report.anchor_journal == pytest.approx(2_500.0)
    assert report.anchor_drift_pct == pytest.approx(0.05)
    assert not any("dérive d'ancre" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_large_anchor_drift_warns(store: StateStore) -> None:
    await store.kv_set("anchor_price", "2500.0")
    snap = _snap(mark_price=2_500.0 * 1.20)  # +20 %, above threshold
    report = await reconcile_at_boot(store, snap)
    assert any("dérive d'ancre" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_debt_drift_warns(store: StateStore) -> None:
    await store.kv_set("debt_usd_last", "35000.00")
    snap = _snap(usdc_variable_debt_balance=40_000.0)
    report = await reconcile_at_boot(store, snap)
    assert any("dette on-chain" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_low_hf_critical(store: StateStore) -> None:
    snap = replace(_snap(), hf=1.05)
    report = await reconcile_at_boot(store, snap)
    assert any("HF observé" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_emode_nonzero_warns(store: StateStore) -> None:
    snap = _snap(aave_emode=1)
    report = await reconcile_at_boot(store, snap)
    assert any("e-mode" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_debt_last_written_after_reconcile(store: StateStore) -> None:
    await reconcile_at_boot(store, _snap())
    value = await store.kv_get("debt_usd_last")
    assert value is not None
    assert float(value) == pytest.approx(35_000.0)
