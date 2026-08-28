"""TRACER loop — the M1 marche à blanc.

Per README §14:
  "Pendant M1, le moteur de décision tourne sur données réelles et journalise
   les actions qu'il aurait prises (journal des tirs à blanc, relu en revue M1)."

Two independent streams run in the same loop:
1. **Shadow journal**: every cycle, `watcher.snapshot() -> decide() -> journal
   if non-NOOP`. Zero side-effects, always active.
2. **Micro-op scheduler** (opt-in, M1-B2): if executors are provided, fires
   Aave / HL / bridge tracer round-trips on config-driven intervals. Each
   round-trip measures the real latency of a critical path (README §7).

The micro-op stream is DISABLED by default (executors default to None) so
DRY_RUN tracer runs continue to observe without any real transaction.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from delta0.config import Config
from delta0.decision import BlindState, OperationalContext, decide
from delta0.executor import AaveTraceExecutor
from delta0.hl_executor import HLTraceExecutor
from delta0.logging import get_logger, set_cycle_id
from delta0.safety import SafetyRefused
from delta0.state import StateStore
from delta0.types import Snapshot
from delta0.venues.bridge import BridgeExecutor
from delta0.venues.hl_stream import HyperliquidStream
from delta0.watchdog import KillSignal, Watchdog
from delta0.watcher import WatcherProtocol

log = get_logger(__name__)

# Latency paths tracked (README §7 chemin column).
LATENCY_PATH_SNAPSHOT = "snapshot"
LATENCY_PATH_DECISION = "decision"

# USDC address on Arbitrum — pulled from config in practice; here as a
# fallback for callers that instantiate a bare TracerLoop.
_USDC_ARB_MAINNET = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


@dataclass(slots=True)
class TracerLoop:
    watcher: WatcherProtocol
    watchdog: Watchdog
    store: StateStore
    config: Config
    cadence_s: float = 5.0
    stream: HyperliquidStream | None = None
    # Opt-in micro-op executors. None => shadow-journal-only tracer (M1
    # phase A behavior). Provide them to enable the M1-B2 latency measurements
    # of the 5 critical paths.
    aave_executor: AaveTraceExecutor | None = None
    hl_executor: HLTraceExecutor | None = None
    bridge_executor: BridgeExecutor | None = None

    # Scheduler state (last-fired monotonic timestamps per micro-op kind).
    _last_aave_cycle: float = field(default=0.0)
    _last_hl_cancel: float = field(default=0.0)
    _last_bridge_cycle: float = field(default=0.0)

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

            # --- Scheduled micro-ops (opt-in) --------------------------------
            await self._maybe_fire_micro_ops(now_mono=time.monotonic())

            # --- Wait -------------------------------------------------------
            await asyncio.sleep(self.cadence_s)

        return shadow_count

    async def _maybe_fire_micro_ops(self, *, now_mono: float) -> None:
        """Fire scheduled micro-ops when their interval has elapsed.

        Each executor is guarded by its own safety guard (allowlist, cap,
        rate limit, KILL file, first-use). If a guard refuses, the loop
        continues — a scheduled micro-op is best-effort, never mandatory.
        """
        tracer_cfg = self.config.tracer

        if (
            self.aave_executor is not None
            and now_mono - self._last_aave_cycle >= tracer_cfg.aave_cycle_every_s
        ):
            self._last_aave_cycle = now_mono
            await self._fire_aave_cycle()

        if (
            self.hl_executor is not None
            and now_mono - self._last_hl_cancel >= tracer_cfg.hl_cancel_every_s
        ):
            self._last_hl_cancel = now_mono
            await self._fire_hl_cancel()

        if (
            self.bridge_executor is not None
            and now_mono - self._last_bridge_cycle >= tracer_cfg.bridge_every_s
        ):
            self._last_bridge_cycle = now_mono
            await self._fire_bridge_round_trip()

    async def _fire_aave_cycle(self) -> None:
        """Approve + supply + repay + withdraw of `aave_cycle_amount_usdc`.

        Each of the four ops records its own latency via the executor. If any
        step raises SafetyRefused the cycle is aborted but the loop lives.
        """
        assert self.aave_executor is not None
        amount = self.config.tracer.aave_cycle_amount_usdc
        usdc = self.config.venues.usdc_address or _USDC_ARB_MAINNET
        try:
            await self.aave_executor.approve(usdc, amount)
            await self.aave_executor.supply(usdc, amount)
            await self.aave_executor.repay(usdc, amount)
            await self.aave_executor.withdraw(usdc, amount)
            log.info(
                "aave_cycle_ok",
                message=f"cycle Aave complet ({amount} USDC) OK",
                amount=amount,
            )
        except SafetyRefused as e:
            log.warning(
                "aave_cycle_refused",
                message=f"cycle Aave refusé par le guard: {e}",
            )
        except Exception:
            log.exception(
                "aave_cycle_failed",
                message="cycle Aave a levé une exception — journal des intents à relire",
            )

    async def _fire_hl_cancel(self) -> None:
        """One HL post-only + cancel round-trip."""
        assert self.hl_executor is not None
        try:
            await self.hl_executor.post_and_cancel(side="sell")
        except SafetyRefused as e:
            log.warning(
                "hl_cancel_refused",
                message=f"hl_post_only_cancel refusé par le guard: {e}",
            )
        except Exception:
            log.exception(
                "hl_cancel_failed",
                message="hl_post_only_cancel a levé une exception",
            )

    async def _fire_bridge_round_trip(self) -> None:
        """One bridge round-trip (~10-15 min wall time in live mode)."""
        assert self.bridge_executor is not None
        amount = self.config.tracer.bridge_amount_usdc
        try:
            await self.bridge_executor.round_trip(amount)
            log.info(
                "bridge_round_trip_ok",
                message=f"aller-retour pont complet ({amount} USDC) OK",
                amount=amount,
            )
        except SafetyRefused as e:
            log.warning(
                "bridge_round_trip_refused",
                message=f"bridge round_trip refusé par le guard: {e}",
            )
        except Exception:
            log.exception(
                "bridge_round_trip_failed",
                message="bridge round_trip a levé une exception",
            )

    async def _build_context(self, snap: Snapshot) -> OperationalContext:
        anchor_str = await self.store.kv_get("anchor_price")
        anchor = float(anchor_str) if anchor_str else None
        last_skim_str = await self.store.kv_get("last_skim_at")
        last_skim = datetime.fromisoformat(last_skim_str) if last_skim_str else None
        blind = self.watchdog.blind_state()
        liquidation = self._check_liquidation_events()

        # Regime-gate inputs are left as None in M1: P10 requires a 30-day
        # funding average with 7-day hysteresis (README §8.9). The regime
        # evaluator lands in M2 alongside the historical funding pipeline.
        # Feeding a naive `desired = config.exposure_mult` here would fire
        # P10 spuriously because the cushion inflates equity above the
        # bare wstETH leg.
        return OperationalContext(
            now_utc=datetime.now(UTC),
            blind_state=blind if isinstance(blind, BlindState) else BlindState.NOMINAL,
            liquidation_event=liquidation,
            anchor_price=anchor,
            last_skim_at=last_skim,
            desired_exposure_mult=None,
            current_exposure_mult=None,
        )

    def _check_liquidation_events(self) -> bool:
        """Drain the HL user-event queue and return True if a liquidation is
        pending. Also logs any fill/funding event for the digest.

        Aave-side LiquidationCall detection is a M2 concern (event filter on
        the Pool address); M1 wires the HL side.
        """
        if self.stream is None:
            return False
        events = self.stream.drain_user_events()
        seen_liquidation = False
        for evt in events:
            if evt.kind == "liquidation":
                seen_liquidation = True
                log.critical(
                    "hl_liquidation",
                    message="liquidation Hyperliquid détectée sur notre compte",
                    raw=evt.raw,
                )
            elif evt.kind == "fill":
                log.info(
                    "hl_fill",
                    message="fill Hyperliquid observé",
                    raw=evt.raw,
                )
        return seen_liquidation
