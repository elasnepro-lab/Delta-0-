"""Persistent state — SQLite journal.

Tables:
- `intents`         : execution intents (pending -> sent -> confirmed | failed).
- `shadow_intents`  : what the decision engine would have done in TRACER mode
                      (M1). Never executed. The primary output of the marche à
                      blanc, per README §14.
- `state_kv`        : simple key/value bag (anchor_price, regime, mode, ...).
- `transfers`       : bridge transfers in-flight.
- `latencies`       : samples for the rolling watchdog histogram.

All access is async via aiosqlite. Everything is written before the first
network call; reconciliation on restart reads the journal. See README §13.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from delta0.types import Action

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    action       TEXT NOT NULL,
    priority     INTEGER NOT NULL,
    params_json  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','sent','confirmed','failed')),
    tx_hashes    TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_created ON intents(created_at);

CREATE TABLE IF NOT EXISTS shadow_intents (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    action       TEXT NOT NULL,
    priority     INTEGER NOT NULL,
    params_json  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    snapshot_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_priority ON shadow_intents(priority);
CREATE INDEX IF NOT EXISTS idx_shadow_created  ON shadow_intents(created_at);

CREATE TABLE IF NOT EXISTS state_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('bridge_out','bridge_in')),
    amount_usdc  REAL NOT NULL,
    started_at   TEXT NOT NULL,
    credited_at  TEXT,
    tx_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_pending
    ON transfers(credited_at) WHERE credited_at IS NULL;

CREATE TABLE IF NOT EXISTS latencies (
    ts          TEXT NOT NULL,
    path        TEXT NOT NULL,
    duration_ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lat_path_ts ON latencies(path, ts);
"""


@dataclass(frozen=True, slots=True)
class ShadowIntent:
    """A decision that WOULD have been executed in LIVE mode (TRACER)."""

    id: str
    created_at: datetime
    action: str
    priority: int
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    snapshot_ts: datetime = field(default_factory=lambda: datetime.now(UTC))


def deterministic_id(*parts: str) -> str:
    """Deterministic short hash — required for idempotence (README §13)."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class StateStore:
    """Async SQLite wrapper. One connection per bot process.

    Not a full ORM — the surface is intentionally narrow so any SQL that runs
    can be reasoned about in one place.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> Any:
        assert self._conn is not None, "StateStore not opened"
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    # --- Shadow intents (TRACER) ----------------------------------------------

    async def record_shadow_intent(self, action: Action, snapshot_ts: datetime) -> str:
        assert self._conn is not None, "StateStore not opened"
        now = datetime.now(UTC)
        intent_id = deterministic_id(
            action.kind,
            str(action.priority.value),
            snapshot_ts.isoformat(),
            json.dumps(action.params, sort_keys=True, default=str),
        )
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO shadow_intents
                (id, created_at, action, priority, params_json, reason, snapshot_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                now.isoformat(),
                action.kind,
                int(action.priority.value),
                json.dumps(action.params, sort_keys=True, default=str),
                action.reason,
                snapshot_ts.isoformat(),
            ),
        )
        await self._conn.commit()
        return intent_id

    async def count_shadow_intents(self) -> int:
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute("SELECT COUNT(*) FROM shadow_intents") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def shadow_intents_by_priority(self) -> dict[int, int]:
        """Histogram of triggered priorities in TRACER mode."""
        assert self._conn is not None, "StateStore not opened"
        result: dict[int, int] = {}
        async with self._conn.execute(
            "SELECT priority, COUNT(*) FROM shadow_intents GROUP BY priority",
        ) as cur:
            async for row in cur:
                result[int(row[0])] = int(row[1])
        return result

    # --- KV store -------------------------------------------------------------

    async def kv_set(self, key: str, value: str) -> None:
        assert self._conn is not None, "StateStore not opened"
        await self._conn.execute(
            """
            INSERT INTO state_kv (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def kv_get(self, key: str) -> str | None:
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute(
            "SELECT value FROM state_kv WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
            return str(row[0]) if row else None

    # --- Latencies ------------------------------------------------------------

    async def record_latency(self, path: str, duration_ms: float) -> None:
        assert self._conn is not None, "StateStore not opened"
        await self._conn.execute(
            "INSERT INTO latencies (ts, path, duration_ms) VALUES (?, ?, ?)",
            (datetime.now(UTC).isoformat(), path, duration_ms),
        )
        await self._conn.commit()

    async def latency_stats(self, path: str) -> dict[str, float]:
        """Return count / p50 / p95 for a path.

        Simple SQL-side aggregation — we sort all samples in memory. Fine for
        M1 sample sizes (tens of thousands at most). If it ever hurts, move
        to an approximate histogram.
        """
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute(
            "SELECT duration_ms FROM latencies WHERE path = ? ORDER BY duration_ms",
            (path,),
        ) as cur:
            values = [float(r[0]) async for r in cur]
        if not values:
            return {"count": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        n = len(values)
        p50 = values[max(0, int(0.50 * n) - 1)]
        p95 = values[max(0, int(0.95 * n) - 1)]
        return {"count": float(n), "p50": p50, "p95": p95, "max": values[-1]}

    async def latency_paths(self) -> list[str]:
        """Every path name with at least one recorded sample, sorted.

        The report uses this instead of a hardcoded list: an executor that
        starts recording a new path shows up in the report on its own.
        """
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute(
            "SELECT DISTINCT path FROM latencies ORDER BY path",
        ) as cur:
            return [str(row[0]) async for row in cur]

    async def latency_stats_all(self) -> dict[str, dict[str, float]]:
        """`latency_stats` for every recorded path, keyed by path name."""
        return {path: await self.latency_stats(path) for path in await self.latency_paths()}
