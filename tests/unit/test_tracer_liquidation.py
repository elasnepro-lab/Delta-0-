"""TracerLoop drains HL liquidation events into the decision context."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from delta0.config import Config
from delta0.state import StateStore
from delta0.tracer import TracerLoop
from delta0.types import Snapshot
from delta0.venues.hl_stream import HyperliquidStream, UserEvent
from delta0.watchdog import Watchdog


def _base_snap() -> Snapshot:
    return Snapshot(
        ts=datetime.now(UTC),
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


@dataclass(slots=True)
class _FakeWatcher:
    snapshot_val: Snapshot

    async def snapshot(self) -> Snapshot:
        return self.snapshot_val


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_tracer_journals_liquidation_response(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    # Prime the stream with a liquidation event.
    stream = HyperliquidStream(
        api_url="https://api.hyperliquid.xyz",
        user_address="0x0",
        coin="ETH",
    )
    stream._loop = asyncio.get_running_loop()
    stream._events_queue.put_nowait(
        UserEvent(kind="liquidation", raw={"px": "2500"}, ts_monotonic=0.0),
    )

    watcher = _FakeWatcher(snapshot_val=_base_snap())
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
        stream=stream,
    )

    n = await loop.run(duration_s=0.01)
    assert n >= 1
    hist = await store.shadow_intents_by_priority()
    assert 1 in hist  # P1 fired


@pytest.mark.asyncio
async def test_tracer_ignores_non_liquidation_events(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    stream = HyperliquidStream(
        api_url="https://api.hyperliquid.xyz",
        user_address="0x0",
        coin="ETH",
    )
    stream._loop = asyncio.get_running_loop()
    stream._events_queue.put_nowait(
        UserEvent(kind="fill", raw={"sz": "1.0"}, ts_monotonic=0.0),
    )
    # Anchor set so no recenter fires; delta 0 so no P8 either.
    await store.kv_set("anchor_price", "2500.0")

    watcher = _FakeWatcher(snapshot_val=_base_snap())
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
        stream=stream,
    )
    n = await loop.run(duration_s=0.005)
    assert n == 0
    assert await store.count_shadow_intents() == 0
