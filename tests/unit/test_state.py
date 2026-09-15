"""SQLite state store — schema, shadow intents, KV, latencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
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


@pytest.mark.asyncio
async def test_failure_cause_roundtrip(store: StateStore) -> None:
    async with store.transaction() as conn:
        await conn.execute(
            """INSERT INTO intents
               (id, created_at, action, priority, params_json, reason, status, updated_at)
               VALUES ('i1', '2026-09-12T00:00:00+00:00', 'aave_repay', 3, '{}',
                       'micro-op M1-B2', 'failed', '2026-09-12T00:00:00+00:00')""",
        )
    assert await store.intent_failure("i1") is None
    await store.record_intent_failure("i1", "revert | Boom | 0xdeadbeef")
    assert await store.intent_failure("i1") == "revert | Boom | 0xdeadbeef"


@pytest.mark.asyncio
async def test_failure_of_an_unknown_intent_is_none(store: StateStore) -> None:
    assert await store.intent_failure("nope") is None


@pytest.mark.asyncio
async def test_the_failure_column_reaches_a_journal_created_without_it(
    tmp_path: Path,
) -> None:
    """A campaign's journal is the one we most want to read back.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so without
    the additive step a new column would never appear in `data/m1_run.db` and
    every past failure would stay unreadable.
    """
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """CREATE TABLE intents (
                   id TEXT PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL,
                   priority INTEGER NOT NULL, params_json TEXT NOT NULL,
                   reason TEXT NOT NULL, status TEXT NOT NULL, tx_hashes TEXT,
                   updated_at TEXT NOT NULL)""",
        )
        await conn.execute(
            """INSERT INTO intents VALUES
               ('old', '2026-09-08T20:39:53+00:00', 'aave_repay', 3, '{}',
                'micro-op M1-B2', 'failed', NULL, '2026-09-08T20:39:53+00:00')""",
        )
        await conn.commit()

    s = StateStore(path)
    await s.open()
    try:
        # The pre-existing row survived, and can now carry a cause.
        await s.record_intent_failure("old", "revert_gas | tx=0xabc")
        assert await s.intent_failure("old") == "revert_gas | tx=0xabc"
        # Opening twice must not attempt the column a second time.
        await s.open()
    finally:
        await s.close()
