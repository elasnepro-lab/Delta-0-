"""HLTraceExecutor — post-and-cancel round trip, dry-run + live paths."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from delta0.config import Config
from delta0.hl_executor import HLTraceExecutor
from delta0.safety import MicroOpsGuard, SafetyRefused
from delta0.state import StateStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


def _make_executor(
    tmp_path: Path,
    store: StateStore,
    config: Config,
    *,
    dry_run: bool = True,
    require_confirm: bool = False,
) -> tuple[HLTraceExecutor, MicroOpsGuard, MagicMock]:
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={
                    "dry_run": dry_run,
                    "require_first_use_confirmation": require_confirm,
                },
            ),
        },
    )
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    if not require_confirm:
        guard.confirm_kind("hl_post_only_cancel")

    exchange_mock = MagicMock()
    exchange_mock.order.return_value = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": 12345}}]},
        },
    }
    exchange_mock.cancel.return_value = {"status": "ok"}

    async def _fake_mark(coin: str) -> float:
        return 2500.0

    executor = HLTraceExecutor(
        config=cfg,
        store=store,
        guard=guard,
        exchange_factory=lambda: exchange_mock,
        get_mark_price=_fake_mark,
    )
    return executor, guard, exchange_mock


@pytest.mark.asyncio
async def test_dry_run_post_and_cancel_records_latency(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, exchange = _make_executor(tmp_path, store, config, dry_run=True)
    result = await executor.post_and_cancel()
    assert result.status == "dry_run"
    assert result.order_id is None
    assert result.fill_size == 0.0
    # No SDK call in dry-run.
    exchange.order.assert_not_called()
    exchange.cancel.assert_not_called()
    stats = await store.latency_stats("path.p1_p2_hl_local")
    assert stats["count"] == 1


@pytest.mark.asyncio
async def test_live_run_places_alo_and_cancels(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, exchange = _make_executor(tmp_path, store, config, dry_run=False)
    result = await executor.post_and_cancel(side="sell")
    assert result.status == "confirmed"
    assert result.order_id == 12345
    assert result.fill_size == 0.0

    # Sanity on the order args: post-only (Alo tif), sell above mark, size ~ 12/2500.
    args, kwargs = exchange.order.call_args
    _ = kwargs
    coin, is_buy, size, limit_price, order_type = args
    assert coin == "ETH"
    assert is_buy is False
    assert size == pytest.approx(12.0 / 2500.0)
    assert limit_price == pytest.approx(2500.0 * 1.10)
    assert order_type == {"limit": {"tif": "Alo"}}

    # Cancel called with matching order id.
    exchange.cancel.assert_called_once_with("ETH", 12345)


@pytest.mark.asyncio
async def test_buy_side_prices_below_mark(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, exchange = _make_executor(tmp_path, store, config, dry_run=False)
    await executor.post_and_cancel(side="buy")
    args, _ = exchange.order.call_args
    _, is_buy, _, limit_price, _ = args
    assert is_buy is True
    assert limit_price == pytest.approx(2500.0 * 0.90)


@pytest.mark.asyncio
async def test_first_use_gate_blocks(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, guard, _ = _make_executor(
        tmp_path,
        store,
        config,
        dry_run=True,
        require_confirm=True,
    )
    with pytest.raises(SafetyRefused, match="confirmation"):
        await executor.post_and_cancel()
    guard.confirm_kind("hl_post_only_cancel")
    result = await executor.post_and_cancel()
    assert result.status == "dry_run"


@pytest.mark.asyncio
async def test_kill_file_blocks(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, _ = _make_executor(tmp_path, store, config, dry_run=True)
    (tmp_path / "KILL").write_text("")
    with pytest.raises(SafetyRefused, match="KILL"):
        await executor.post_and_cancel()


@pytest.mark.asyncio
async def test_partial_fill_triggers_warning_but_still_completes(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, exchange = _make_executor(tmp_path, store, config, dry_run=False)
    # Simulate a filled response with totalSz > 0.
    exchange.order.return_value = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"oid": 42, "totalSz": 0.001}}]},
        },
    }
    result = await executor.post_and_cancel()
    assert result.status == "confirmed"
    assert result.fill_size == pytest.approx(0.001)
    # Cancel still called on the filled order's oid.
    exchange.cancel.assert_called_once_with("ETH", 42)


@pytest.mark.asyncio
async def test_extract_order_id_handles_malformed(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _, exchange = _make_executor(tmp_path, store, config, dry_run=False)
    exchange.order.return_value = {"weird": "shape"}
    result = await executor.post_and_cancel()
    assert result.order_id is None
    # No cancel call when order id is unknown.
    exchange.cancel.assert_not_called()
    # Still confirmed status because no exception raised.
    assert result.status == "confirmed"
