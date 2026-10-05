"""TRACER loop — the M1 marche à blanc.

Per README §14:
  "Pendant M1, le moteur de décision tourne sur données réelles et journalise
   les actions qu'il aurait prises (journal des tirs à blanc, relu en revue M1)."

Two independent streams run in the same loop:
1. **Shadow journal**: every cycle, `watcher.snapshot() -> decide() -> journal
   if non-NOOP`, then the invariants on the same snapshot. Zero side-effects,
   always active.
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
from delta0.failure import OPERATIONAL_ERRORS
from delta0.hl_executor import HLTraceExecutor
from delta0.invariants import (
    InvariantContext,
    InvariantVerdict,
    assess,
    breach_since,
    check_invariants,
    cushion_breached,
    emit,
    margin_breached,
)
from delta0.latency import elapsed_ms, now_perf
from delta0.logging import get_logger, set_cycle_id
from delta0.safety import SafetyRefused
from delta0.state import StateStore
from delta0.types import Action, Priority, Snapshot
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


def _aave_position_in_the_way(snap: Snapshot) -> str | None:
    """What on the Aave account forbids the tracer's cycle, or None if it is empty.

    The cycle ends with `repay_all` and `withdraw_all`, both MAX_UINT256: they act
    on the whole account, not on the cycle's own few USDC. Next to a real
    position they would repay the strategy's debt and pull its cushion; after a
    cycle that died half-way they would run on top of the leftovers (audit dev
    2026-09-16, 1.9). The docstring of `withdraw_all` named the risk, nothing
    checked it.
    """
    found = []
    if snap.wsteth_atoken_balance > 0.0:
        found.append(f"{snap.wsteth_atoken_balance:.6f} wstETH déposés")
    if snap.usdc_atoken_balance > 0.0:
        found.append(f"{snap.usdc_atoken_balance:.2f} USDC déposés")
    if snap.usdc_variable_debt_balance > 0.0:
        found.append(f"{snap.usdc_variable_debt_balance:.2f} USDC de dette")
    return ", ".join(found) or None


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
    # -inf, not 0.0: the interval test is `now_mono - last >= every_s`, and
    # `time.monotonic()` on Windows counts from boot. With 0.0 the first
    # bridge round-trip only fires if the machine happens to have been up
    # longer than its 12 h interval — so "the first cycle fires within the
    # first seconds" would be true on one machine and false on another.
    # -inf makes the first tick of every kind fire immediately, everywhere.
    _last_aave_cycle: float = field(default=float("-inf"))
    _last_hl_cancel: float = field(default=float("-inf"))
    _last_bridge_cycle: float = field(default=float("-inf"))

    # What the invariants need and a snapshot cannot say (README §11): when
    # each threshold was first crossed, and when each flank last answered.
    _cushion_breach_since: datetime | None = None
    _margin_breach_since: datetime | None = None
    _last_down_defence_at: datetime | None = None
    _last_up_defence_at: datetime | None = None
    last_verdict: InvariantVerdict | None = None

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
            t0 = now_perf()
            try:
                snap = await self.watcher.snapshot()
            except OPERATIONAL_ERRORS:
                # Survive an unreachable venue, never a bug: an AttributeError
                # here used to loop forever as "échec construction snapshot".
                log.exception("snapshot_failed", message="échec construction snapshot")
                await asyncio.sleep(self.cadence_s)
                continue
            snap_ms = elapsed_ms(t0)
            await self.store.record_latency(LATENCY_PATH_SNAPSHOT, snap_ms)

            # --- Decision ---------------------------------------------------
            ctx = await self._build_context(snap)
            t1 = now_perf()
            action = decide(snap, self.config, ctx)
            dec_ms = elapsed_ms(t1)
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

            # --- Invariants --------------------------------------------------
            verdict = self._check_invariants(snap, action, ctx.blind_state)

            # --- Scheduled micro-ops (opt-in) --------------------------------
            # A CRITICAL verdict, or gas under its floor, freezes everything
            # non-critical — and a measuring round-trip is nothing else.
            if verdict.freeze_non_critical:
                log.warning(
                    "micro_ops_frozen",
                    message="micro-opérations suspendues : un invariant gèle le non-critique",
                    level=verdict.level,
                )
            else:
                await self._maybe_fire_micro_ops(now_mono=time.monotonic(), snap=snap)

            # --- Wait -------------------------------------------------------
            await asyncio.sleep(self.cadence_s)

        return shadow_count

    def _check_invariants(
        self, snap: Snapshot, action: Action, blind: BlindState
    ) -> InvariantVerdict:
        """I1-I9 on this cycle's snapshot, with the bookkeeping they depend on.

        They were written in chantier 6.1 and called by nothing: the second
        revue finance (2026-10-05, N3) found I9 — the governance check meant to
        run on every snapshot — reachable from no loop at all. In the tracer a
        defence is the intent `decide()` emitted, since nothing executes it.

        Not tracked here yet: transfers and executions in flight (chantiers
        4.5 and 3.2), so I6 and I7 stay quiet. And the tracer cannot deflate:
        a verdict asking for it is logged CRITICAL by `emit`, nothing more.
        """
        now = snap.ts
        if action.priority in (Priority.P3_EMERGENCY_REPAY, Priority.P4_DELEVERAGE):
            self._last_down_defence_at = now
        if action.priority in (Priority.P1_LIQUIDATION_DETECTED, Priority.P2_EMERGENCY_REDUCE):
            self._last_up_defence_at = now
        self._cushion_breach_since = breach_since(
            self._cushion_breach_since, cushion_breached(snap, self.config), now
        )
        self._margin_breach_since = breach_since(
            self._margin_breach_since, margin_breached(snap, self.config), now
        )
        ctx = InvariantContext(
            now=now,
            # No emergency and both venues in sight: the steady state I1-I3
            # describe, as far as a loop without a state machine can tell.
            cruising=action.kind == "NOOP" and blind is BlindState.NOMINAL,
            cushion_breach_since=self._cushion_breach_since,
            last_down_defence_at=self._last_down_defence_at,
            margin_breach_since=self._margin_breach_since,
            last_up_defence_at=self._last_up_defence_at,
        )
        verdict = assess(check_invariants(snap, self.config, ctx))
        emit(verdict, log)
        self.last_verdict = verdict
        return verdict

    async def _maybe_fire_micro_ops(self, *, now_mono: float, snap: Snapshot) -> None:
        """Fire scheduled micro-ops when their interval has elapsed.

        Each executor is guarded by its own safety guard (allowlist, cap,
        rate limit, KILL file, first-use). If a guard refuses, the loop
        continues — a scheduled micro-op is best-effort, never mandatory.
        `snap` is this cycle's snapshot: what the account holds right now.
        """
        tracer_cfg = self.config.tracer

        if (
            self.aave_executor is not None
            and now_mono - self._last_aave_cycle >= tracer_cfg.aave_cycle_every_s
        ):
            self._last_aave_cycle = now_mono
            self.aave_executor.observe_wsteth_price(snap.wsteth_price_usd)
            await self._fire_aave_cycle(snap)

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

    async def _fire_aave_cycle(self, snap: Snapshot) -> None:
        """Full round trip: approve, supply, borrow, repay-all, withdraw-all.

        Sequence chosen after fork validation (see memory/aave_findings.md):
        - approve gives the Pool permission to pull collateral.
        - supply deposits `amount` USDC as collateral.
        - borrow a fraction (~20 %) so we have debt to close later.
        - approve again a small buffer for the repay (borrow + accrued interest).
        - repay_all closes the entire position via MAX_UINT256 (a partial repay
          would leave dust interest that blocks the withdraw).
        - withdraw_all pulls back the full collateral, also via MAX_UINT256.
          Asking for the exact `amount` supplied reverts whenever Aave's
          scaled-balance rounding lands one unit short (5.000000 supplied reads
          back as 4.999999) — see memory/aave_findings.md.

        Each of the six ops records its own latency via the executor.
        SafetyRefused or any other exception aborts the cycle without killing
        the loop.
        """
        assert self.aave_executor is not None
        in_the_way = _aave_position_in_the_way(snap)
        if in_the_way is not None:
            log.warning(
                "aave_cycle_refused_position",
                message=(
                    f"cycle Aave refusé : le compte porte déjà {in_the_way}. "
                    "repay_all et withdraw_all agiraient sur tout le compte."
                ),
            )
            return
        amount = self.config.tracer.aave_cycle_amount_usdc
        usdc = self.config.venues.usdc_address or _USDC_ARB_MAINNET
        # Borrow ~20 % of the supplied amount to keep well below the LTV limit.
        borrow_amount = round(amount * 0.20, 6)
        repay_headroom = round(borrow_amount + 1.0, 6)
        try:
            await self.aave_executor.approve(usdc, amount)
            await self.aave_executor.supply(usdc, amount)
            await self.aave_executor.borrow(usdc, borrow_amount)
            await self.aave_executor.approve(usdc, repay_headroom)
            await self.aave_executor.repay_all(usdc)
            await self.aave_executor.withdraw_all(usdc, amount)
            log.info(
                "aave_cycle_ok",
                message=f"cycle Aave complet ({amount} USDC) OK",
                amount=amount,
                borrow_amount=borrow_amount,
            )
        except SafetyRefused as e:
            log.warning(
                "aave_cycle_refused",
                message=f"cycle Aave refusé par le guard: {e}",
            )
        except OPERATIONAL_ERRORS:
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
        except OPERATIONAL_ERRORS:
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
        except OPERATIONAL_ERRORS:
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
