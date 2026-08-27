"""TRACER loop — the M1 marche à blanc.

Per README §14:
  "Pendant M1, le moteur de décision tourne sur données réelles et journalise
   les actions qu'il aurait prises (journal des tirs à blanc, relu en revue M1)."

This module orchestrates: watcher -> pure decision -> shadow-journal + latency
measurement. It NEVER executes an action. That is a hard invariant of M1.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from delta0.config import Config
from delta0.decision import BlindState, OperationalContext, decide
from delta0.logging import get_logger, set_cycle_id
from delta0.state import StateStore
from delta0.types import Snapshot
from delta0.watchdog import KillSignal, Watchdog
from delta0.watcher import WatcherProtocol

log = get_logger(__name__)

# Latency paths tracked (README §7 chemin column).
LATENCY_PATH_SNAPSHOT = "snapshot"
LATENCY_PATH_DECISION = "decision"


@dataclass(slots=True)
class TracerLoop:
    watcher: WatcherProtocol
    watchdog: Watchdog
    store: StateStore
    config: Config
    cadence_s: float = 5.0

    async def run(self, duration_s: float | None = None) -> int:
        """Run the TRACER loop for `duration_s` (or forever if None).

        Returns the number of shadow intents journaled.
        """
        deadline = time.monotonic() + duration_s if duration_s else None
        shadow_count = 0
        cycle = 0

        while True:
            cycle += 1
            set_cycle_id(f"cyc-{cycle:06d}")

            if deadline is not None and time.monotonic() >= deadline:
                log.info("tracer_end", message="durée écoulée — arrêt propre")
                break

            kill = self.watchdog.kill_signal()
            if kill is not KillSignal.NONE:
                log.warning(
                    "tracer_kill",
                    message=f"signal opérateur détecté ({kill}) — arrêt propre",
                    signal=str(kill),
                )
                break

            # --- Snapshot ---------------------------------------------------
            t0 = time.monotonic()
            try:
                snap = await self.watcher.snapshot()
            except Exception:
                log.exception("snapshot_failed", message="échec construction snapshot")
                await asyncio.sleep(self.cadence_s)
                continue
            snap_ms = (time.monotonic() - t0) * 1000.0
            await self.store.record_latency(LATENCY_PATH_SNAPSHOT, snap_ms)

            # --- Decision ---------------------------------------------------
            ctx = await self._build_context(snap)
            t1 = time.monotonic()
            action = decide(snap, self.config, ctx)
            dec_ms = (time.monotonic() - t1) * 1000.0
            await self.store.record_latency(LATENCY_PATH_DECISION, dec_ms)

            if action.kind != "NOOP":
                await self.store.record_shadow_intent(action, snap.ts)
                shadow_count += 1
                log.info(
                    "shadow_intent",
                    message=f"décision journalisée: {action.kind} (P{action.priority.value})",
                    action=action.kind,
                    priority=action.priority.value,
                    reason=action.reason,
                )

            # --- Wait -------------------------------------------------------
            await asyncio.sleep(self.cadence_s)

        return shadow_count

    async def _build_context(self, snap: Snapshot) -> OperationalContext:
        anchor_str = await self.store.kv_get("anchor_price")
        anchor = float(anchor_str) if anchor_str else None
        last_skim_str = await self.store.kv_get("last_skim_at")
        last_skim = datetime.fromisoformat(last_skim_str) if last_skim_str else None
        blind = self.watchdog.blind_state()

        # Regime-gate inputs are left as None in M1: P10 requires a 30-day
        # funding average with 7-day hysteresis (README §8.9). The regime
        # evaluator lands in M2 alongside the historical funding pipeline.
        # Feeding a naive `desired = config.exposure_mult` here would fire
        # P10 spuriously because the cushion inflates equity above the
        # bare wstETH leg.
        return OperationalContext(
            now_utc=datetime.now(UTC),
            blind_state=blind if isinstance(blind, BlindState) else BlindState.NOMINAL,
            liquidation_event=False,  # M1-B: hook to LiquidationCall event stream
            anchor_price=anchor,
            last_skim_at=last_skim,
            desired_exposure_mult=None,
            current_exposure_mult=None,
        )


