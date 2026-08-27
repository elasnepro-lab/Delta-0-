"""Persistent state — SQLite journal for intents, transfers, metrics.

M0: schema definition only. Full wiring in M2/M3. See README section 13.
"""

from __future__ import annotations

# Schema (informational — created by the M2 migration script):
#
#   CREATE TABLE intents (
#       id           TEXT PRIMARY KEY,       -- deterministic hash
#       created_at   TEXT NOT NULL,
#       action       TEXT NOT NULL,
#       priority     INTEGER NOT NULL,
#       params       TEXT NOT NULL,          -- JSON
#       status       TEXT NOT NULL,          -- pending | sent | confirmed | failed
#       tx_hashes    TEXT,                   -- JSON array
#       updated_at   TEXT NOT NULL
#   );
#
#   CREATE TABLE state (
#       key          TEXT PRIMARY KEY,
#       value        TEXT NOT NULL,
#       updated_at   TEXT NOT NULL
#   );
#   -- keys: anchor_price, regime, mode, capital_current
#
#   CREATE TABLE transfers (
#       id           TEXT PRIMARY KEY,
#       kind         TEXT NOT NULL,          -- bridge_out | bridge_in
#       amount_usdc  REAL NOT NULL,
#       started_at   TEXT NOT NULL,
#       credited_at  TEXT,
#       tx_hash      TEXT
#   );
#
#   CREATE TABLE metrics (
#       ts           TEXT NOT NULL,
#       name         TEXT NOT NULL,
#       value        REAL NOT NULL,
#       PRIMARY KEY (ts, name)
#   );
