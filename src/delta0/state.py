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
    updated_at   TEXT NOT NULL,
    failure      TEXT
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
        await self._add_missing_columns()
        await self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def _add_missing_columns(self) -> None:
        """Add nullable columns that a database created earlier does not have.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
        column never reaches a journal that already holds a campaign — and the
        journals we most want to read are exactly the old ones. A real
        migration runner with `SCHEMA_VERSION` honoured is chantier 4.6; until
        then, adding a nullable column is idempotent and cannot lose a row.
        """
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute("PRAGMA table_info(intents)") as cur:
            existing = {str(row[1]) async for row in cur}
        if "failure" not in existing:
            await self._conn.execute("ALTER TABLE intents ADD COLUMN failure TEXT")

    async def record_intent_failure(self, intent_id: str, cause: str) -> None:
        """Attach the cause of a failure to an intent already marked failed.

        Kept out of `_mark_intent_status` on purpose: that helper is duplicated
        across the three executors and chantier 4.1 is going to collapse them.
        One method here means that refactor cannot drop the cause on the way.
        """
        assert self._conn is not None, "StateStore not opened"
        await self._conn.execute(
            "UPDATE intents SET failure = ?, updated_at = ? WHERE id = ?",
            (cause, datetime.now(UTC).isoformat(), intent_id),
        )
        await self._conn.commit()

    async def failure_summary(self, since: str | None = None) -> list[tuple[str, str, int, str]]:
        """Failed intents grouped by action and cause, newest example first.

        Grouped on purpose. The M1 campaign held 128 failures of which 123 were
        the same cause repeating every thirty minutes: a flat list buries the
        five that were distinct, which are the ones worth reading.
        """
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute(
            """SELECT action,
                      COALESCE(failure, 'cause non enregistrée'),
                      COUNT(*)   AS n,
                      MAX(created_at) AS last_seen
                 FROM intents
                WHERE status = 'failed'
             GROUP BY action, COALESCE(failure, 'cause non enregistrée')
             ORDER BY n DESC, last_seen DESC"""
            if since is None
            else """SELECT action,
                           COALESCE(failure, 'cause non enregistrée'),
                           COUNT(*)   AS n,
                           MAX(created_at) AS last_seen
                      FROM intents
                     WHERE status = 'failed' AND created_at >= ?
                  GROUP BY action, COALESCE(failure, 'cause non enregistrée')
                  ORDER BY n DESC, last_seen DESC""",
            () if since is None else (since,),
        ) as cur:
            return [(str(r[0]), str(r[1]), int(r[2]), str(r[3])) async for r in cur]

    async def intent_failure(self, intent_id: str) -> str | None:
        """The recorded cause, or None when the intent did not fail."""
        assert self._conn is not None, "StateStore not opened"
        async with self._conn.execute(
            "SELECT failure FROM intents WHERE id = ?",
            (intent_id,),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None or row[0] is None else str(row[0])

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

    async def count_shadow_intents(self, since: str | None = None) -> int:
        assert self._conn is not None, "StateStore not opened"
        sql = "SELECT COUNT(*) FROM shadow_intents"
        params: tuple[str, ...] = ()
        if since is not None:
            sql += " WHERE created_at >= ?"
            params = (since,)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def shadow_intents_by_priority(self, since: str | None = None) -> dict[int, int]:
        """Histogram of triggered priorities in TRACER mode."""
        assert self._conn is not None, "StateStore not opened"
        result: dict[int, int] = {}
        sql = "SELECT priority, COUNT(*) FROM shadow_intents"
        params: tuple[str, ...] = ()
        if since is not None:
            sql += " WHERE created_at >= ?"
            params = (since,)
        async with self._conn.execute(sql + " GROUP BY priority", params) as cur:
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

    async def latency_stats(self, path: str, since: str | None = None) -> dict[str, float]:
        """Return count / p50 / p95 for a path, optionally over a window.

        Simple SQL-side aggregation — we sort all samples in memory. Fine for
        M1 sample sizes (tens of thousands at most). If it ever hurts, move
        to an approximate histogram.

        `since` is an ISO timestamp. Without it the whole journal is read,
        which is how the M1 report first described a campaign that was not its
        own: `data/m1_run.db` carried 18,3 % of samples from a session three
        days older, and every maximum in the report came from those.
        """
        assert self._conn is not None, "StateStore not opened"
        sql = "SELECT duration_ms FROM latencies WHERE path = ?"
        params: tuple[str, ...] = (path,)
        if since is not None:
            sql += " AND ts >= ?"
            params = (path, since)
        async with self._conn.execute(sql + " ORDER BY duration_ms", params) as cur:
            values = [float(r[0]) async for r in cur]
        if not values:
            return {"count": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        n = len(values)
        p50 = values[max(0, int(0.50 * n) - 1)]
        p95 = values[max(0, int(0.95 * n) - 1)]
        return {"count": float(n), "p50": p50, "p95": p95, "max": values[-1]}

    async def latency_paths(self, since: str | None = None) -> list[str]:
        """Every path name with at least one recorded sample, sorted.

        The report uses this instead of a hardcoded list: an executor that
        starts recording a new path shows up in the report on its own.
        """
        assert self._conn is not None, "StateStore not opened"
        sql = "SELECT DISTINCT path FROM latencies"
        params: tuple[str, ...] = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            params = (since,)
        async with self._conn.execute(sql + " ORDER BY path", params) as cur:
            return [str(row[0]) async for row in cur]

    async def latency_stats_all(self, since: str | None = None) -> dict[str, dict[str, float]]:
        """`latency_stats` for every recorded path, keyed by path name."""
        return {
            path: await self.latency_stats(path, since) for path in await self.latency_paths(since)
        }
