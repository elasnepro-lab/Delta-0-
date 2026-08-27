"""Watchdog — measures latencies, decides BLIND state, honors KILL files.

README §11:
- BLIND when: WS stale > ws_stale_s, or RPC failed > rpc_fail_s,
  or tx_fail_max consecutive failed tx.
- HL_ONLY / AAVE_ONLY when only one venue is reachable.
- Rolling p50/p95 over 7 days per critical path.

The watchdog is pure w.r.t. computation, but it *does* read the filesystem
to check for KILL / KILL_DEFLATE files. Filesystem checks are trivially
cheap and testable via `Path` injection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from delta0.config import WatchdogConfig
from delta0.decision import BlindState


class KillSignal(StrEnum):
    """Runtime signals from the operator via files at the project root."""

    NONE = "NONE"
    PAUSE = "PAUSE"  # KILL file present
    DEFLATE = "DEFLATE"  # KILL_DEFLATE file present


@dataclass(slots=True)
class VenueHealth:
    """Reachability state of one venue."""

    last_ok_at: float = 0.0
    consecutive_failures: int = 0

    def record_ok(self, now: float) -> None:
        self.last_ok_at = now
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def is_healthy(self, now: float, max_silence_s: float, max_failures: int) -> bool:
        if self.consecutive_failures >= max_failures:
            return False
        return (now - self.last_ok_at) <= max_silence_s


@dataclass(slots=True)
class Watchdog:
    """Tracks reachability + latencies. Emits a BlindState verdict per tick."""

    config: WatchdogConfig
    project_root: Path
    _hl: VenueHealth = field(default_factory=VenueHealth)
    _aave: VenueHealth = field(default_factory=VenueHealth)
    _ws_last_tick_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        # Treat "never seen" as "healthy at start" so a fresh boot isn't
        # instantly BLIND before the first tick lands.
        now = time.monotonic()
        self._hl.last_ok_at = now
        self._aave.last_ok_at = now
        self._ws_last_tick_at = now

    # --- Health signal input --------------------------------------------------

    def mark_ws_tick(self, now: float | None = None) -> None:
        self._ws_last_tick_at = now if now is not None else time.monotonic()

    def mark_hl_ok(self, now: float | None = None) -> None:
        self._hl.record_ok(now if now is not None else time.monotonic())

    def mark_hl_failure(self) -> None:
        self._hl.record_failure()

    def mark_aave_ok(self, now: float | None = None) -> None:
        self._aave.record_ok(now if now is not None else time.monotonic())

    def mark_aave_failure(self) -> None:
        self._aave.record_failure()

    # --- Verdict --------------------------------------------------------------

    def ws_stale_seconds(self, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        return now - self._ws_last_tick_at

    def blind_state(self, now: float | None = None) -> BlindState:
        now = now if now is not None else time.monotonic()
        hl_ok = self._hl.is_healthy(
            now,
            max_silence_s=float(self.config.ws_stale_s + self.config.rpc_fail_s),
            max_failures=self.config.tx_fail_max,
        )
        aave_ok = self._aave.is_healthy(
            now,
            max_silence_s=float(self.config.rpc_fail_s),
            max_failures=self.config.tx_fail_max,
        )
        # WS staleness is an HL-side signal too.
        if self.ws_stale_seconds(now) > self.config.ws_stale_s:
            hl_ok = False

        if hl_ok and aave_ok:
            return BlindState.NOMINAL
        if hl_ok and not aave_ok:
            return BlindState.HL_ONLY
        if aave_ok and not hl_ok:
            return BlindState.AAVE_ONLY
        return BlindState.BOTH_BLIND

    def kill_signal(self) -> KillSignal:
        if (self.project_root / "KILL_DEFLATE").exists():
            return KillSignal.DEFLATE
        if (self.project_root / "KILL").exists():
            return KillSignal.PAUSE
        return KillSignal.NONE
