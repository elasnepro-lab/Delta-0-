"""TRACER loop — end-to-end wiring with a fake watcher."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from delta0.config import Config
from delta0.state import StateStore
from delta0.tracer import LATENCY_PATH_DECISION, LATENCY_PATH_SNAPSHOT, TracerLoop
from delta0.types import Snapshot
from delta0.watchdog import Watchdog


@dataclass(slots=True)
class _FakeWatcher:
    snapshots: list[Snapshot]
    _idx: int = 0

    async def snapshot(self) -> Snapshot:
        snap = self.snapshots[min(self._idx, len(self.snapshots) - 1)]
        self._idx += 1
        return snap


def _base_snap() -> Snapshot:
    return Snapshot(
        ts=datetime.now(UTC),
        wsteth_atoken_balance=16.0,
        wsteth_price_usd=3_125.0,
        wsteth_eth_ratio=1.25,
        usdc_atoken_balance=1_000.0,
        usdc_variable_debt_balance=35_000.0,
        usdc_wallet_balance=0.0,
        hf=1.5,
        aave_lt_wsteth=0.83,
        aave_ltv_max_wsteth=0.80,
        aave_emode=0,
        mark_price=2_500.0,
        short_size_eth=20.0,
        isolated_margin_usd=5_000.0,
        hl_free_usdc=1_000.0,
        hl_maintenance_margin=0.02,
        funding_last_hour=1.25e-5,
        funding_30d_annualized=0.11,
        borrow_apr=0.05,
        gas_eth=0.01,
        ws_last_tick_age_s=1.0,
        rpc_ok=True,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_tracer_journals_shadow_intent_when_action_fires(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    # Craft a snapshot that triggers P8 (delta retrue): the staking rate has
    # drifted, so 16 wstETH now stand for 20.64 ETH against a 20 ETH short.
    snap = replace(_base_snap(), wsteth_eth_ratio=1.29)
    watcher = _FakeWatcher(snapshots=[snap])
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
    )
    n = await loop.run(duration_s=0.01)
    assert n >= 1
    hist = await store.shadow_intents_by_priority()
    assert 8 in hist  # P8 fired at least once


@pytest.mark.asyncio
async def test_tracer_stops_on_kill_file(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    watcher = _FakeWatcher(snapshots=[_base_snap()])
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    (tmp_path / "KILL").write_text("")
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
    )
    n = await loop.run(duration_s=10.0)
    assert n == 0
    # No shadow intents were journaled — loop exited on the KILL check.
    assert await store.count_shadow_intents() == 0


@pytest.mark.asyncio
async def test_tracer_records_latency_samples(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    watcher = _FakeWatcher(snapshots=[_base_snap()])
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
    )
    await loop.run(duration_s=0.01)
    snap_stats = await store.latency_stats(LATENCY_PATH_SNAPSHOT)
    dec_stats = await store.latency_stats(LATENCY_PATH_DECISION)
    assert snap_stats["count"] >= 1
    assert dec_stats["count"] >= 1


class _UnreachableWatcher:
    """A venue that does not answer: the loop must ride it out."""

    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self) -> Snapshot:
        self.calls += 1
        raise ConnectionError("RPC injoignable")


class _BrokenWatcher:
    """A bug in the snapshot code: the loop must NOT ride it out."""

    async def snapshot(self) -> Snapshot:
        raise AttributeError("'NoneType' object has no attribute 'mark_price'")


@pytest.mark.asyncio
async def test_the_loop_survives_an_unreachable_venue(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    watcher = _UnreachableWatcher()
    loop = TracerLoop(
        watcher=watcher,
        watchdog=Watchdog(config=config.watchdog, project_root=tmp_path),
        store=store,
        config=config,
        cadence_s=0.0,
    )
    assert await loop.run(duration_s=0.01) == 0
    assert watcher.calls >= 2  # it kept trying, cycle after cycle


@pytest.mark.asyncio
async def test_the_loop_stops_on_a_bug_instead_of_looping_on_it(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """Chantier 6.4: an AttributeError used to loop forever as a failed snapshot."""
    loop = TracerLoop(
        watcher=_BrokenWatcher(),
        watchdog=Watchdog(config=config.watchdog, project_root=tmp_path),
        store=store,
        config=config,
        cadence_s=0.0,
    )
    with pytest.raises(AttributeError, match="mark_price"):
        await loop.run(duration_s=10.0)


@pytest.mark.asyncio
async def test_tracer_does_not_journal_on_noop(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    # A stable snapshot (Monday, no triggers). Anchor kept in KV.
    await store.kv_set("anchor_price", "2500.0")
    watcher = _FakeWatcher(snapshots=[_base_snap()])
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=watcher,
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
    )
    n = await loop.run(duration_s=0.005)
    assert n == 0
    assert await store.count_shadow_intents() == 0
