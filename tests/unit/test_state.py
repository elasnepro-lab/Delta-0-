"""SQLite state store — schema, shadow intents, KV, latencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from delta0.state import StateStore, deterministic_id
from delta0.types import Action, Priority


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_open_creates_schema(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    # Re-open should be idempotent.
    await s.open()
    await s.close()


@pytest.mark.asyncio
async def test_record_shadow_intent_is_idempotent(store: StateStore) -> None:
    snap_ts = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    action = Action(
        kind="REDUCE",
        priority=Priority.P2_EMERGENCY_REDUCE,
        reason="test",
        params={"close_fraction": 0.30, "target_short_size_eth": 14.0},
    )
    id1 = await store.record_shadow_intent(action, snap_ts)
    id2 = await store.record_shadow_intent(action, snap_ts)
    assert id1 == id2
    assert await store.count_shadow_intents() == 1


@pytest.mark.asyncio
async def test_shadow_intents_histogram(store: StateStore) -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    a1 = Action(
        kind="REDUCE",
        priority=Priority.P2_EMERGENCY_REDUCE,
        reason="test",
        params={"n": 1},
    )
    a2 = Action(
        kind="RETRUE_SHORT",
        priority=Priority.P8_DELTA_RETRUE,
        reason="test",
        params={"n": 2},
    )
    await store.record_shadow_intent(a1, now)
    await store.record_shadow_intent(a2, now)
    hist = await store.shadow_intents_by_priority()
    assert hist == {int(Priority.P2_EMERGENCY_REDUCE): 1, int(Priority.P8_DELTA_RETRUE): 1}


@pytest.mark.asyncio
async def test_kv_roundtrip(store: StateStore) -> None:
    assert await store.kv_get("anchor_price") is None
    await store.kv_set("anchor_price", "2500.0")
    assert await store.kv_get("anchor_price") == "2500.0"
    await store.kv_set("anchor_price", "2600.0")
    assert await store.kv_get("anchor_price") == "2600.0"


@pytest.mark.asyncio
async def test_latency_stats_empty(store: StateStore) -> None:
    stats = await store.latency_stats("hl_read")
    assert stats == {"count": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}


@pytest.mark.asyncio
async def test_latency_stats_percentiles(store: StateStore) -> None:
    # 100 samples 1..100 ms.
    for i in range(1, 101):
        await store.record_latency("rpc_call", float(i))
    stats = await store.latency_stats("rpc_call")
    assert stats["count"] == 100.0
    assert stats["p50"] == pytest.approx(50.0)
    assert stats["p95"] == pytest.approx(95.0)
    assert stats["max"] == 100.0


def test_deterministic_id_stable() -> None:
    a = deterministic_id("REDUCE", "2", "ts")
    b = deterministic_id("REDUCE", "2", "ts")
    c = deterministic_id("REDUCE", "3", "ts")
    assert a == b
    assert a != c
    assert len(a) == 16


@pytest.mark.asyncio
async def test_latency_paths_lists_distinct_recorded_paths(store: StateStore) -> None:
    await store.record_latency("path.aave_supply", 10.0)
    await store.record_latency("path.aave_supply", 12.0)
    await store.record_latency("snapshot", 3.0)
    assert await store.latency_paths() == ["path.aave_supply", "snapshot"]


@pytest.mark.asyncio
async def test_latency_paths_is_empty_before_any_sample(store: StateStore) -> None:
    assert await store.latency_paths() == []


@pytest.mark.asyncio
async def test_latency_stats_all_keys_every_path(store: StateStore) -> None:
    await store.record_latency("path.aave_supply", 10.0)
    await store.record_latency("path.p1_p2_hl_local", 400.0)
    stats = await store.latency_stats_all()
    assert set(stats) == {"path.aave_supply", "path.p1_p2_hl_local"}
    assert stats["path.aave_supply"]["count"] == 1.0
    assert stats["path.p1_p2_hl_local"]["p95"] == 400.0
