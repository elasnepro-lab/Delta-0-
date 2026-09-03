"""M1-B2 Hyperliquid post-only + cancel executor.

Executes tiny post-only ALO orders placed FAR from the mark price, then
cancels them immediately. The goal is to measure the round-trip latency of
chemin P1/P2 (local HL, budget 2 s per README §7), not to trade.

Safety design:
- Order price is placed at ±10 % from mark, well outside any realistic fill
  band, so a stray fill is essentially impossible even during volatile
  windows.
- Size is the HL minimum notional (~10 $) — bounded by the safety guard.
- Order is cancelled within milliseconds of placement.
- `dry_run=True` skips network entirely (same pattern as AaveTraceExecutor).

Wallet:
- M1-B2 uses the master wallet directly. Agent-wallet separation
  (README §9.1: agent = sign orders only, no withdrawal rights) lands in M3
  when we go LIVE_SMALL — for now the master wallet signs everything.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from delta0.config import Config
from delta0.hl_api import ensure_ok
from delta0.latency import elapsed_ms, measurement_path, now_perf
from delta0.logging import get_logger
from delta0.safety import MicroOpsGuard
from delta0.state import StateStore, deterministic_id

log = get_logger(__name__)


# HL post-only orders sit 10 % away from mark, well outside any realistic
# fill window even during a flash move. Keep this generous — the point is
# measurement, not tightness.
_POST_ONLY_OFFSET = 0.10

# HL enforces a $10 minimum notional per order. We stay conservative at 12 $
# to avoid rounding-related rejections.
_MIN_NOTIONAL_USD = 12.0
_HL_MIN_NOTIONAL_USD = 10.0

# Hyperliquid refuses any order whose size or price is not exactly
# representable on its wire format — the SDK raises `float_to_wire causes
# rounding` locally, before anything reaches the venue. Two separate rules:
#
#   size  : at most `szDecimals` decimals (per asset, from the exchange meta;
#           ETH is 4).
#   price : at most 5 significant figures AND at most `6 - szDecimals`
#           decimals, whichever binds first. Integer prices are always legal.
#
# A naive `notional / mark` is a full-precision float and satisfies neither,
# which is why the first live order never left the process.
_PERP_MAX_DECIMALS = 6
_PRICE_SIG_FIGS = 5

# Fallback when the exchange meta is not wired in (tests, and any caller that
# does not inject `get_size_decimals`). ETH's value on Hyperliquid.
_ETH_SZ_DECIMALS = 4


def round_size(size: float, sz_decimals: int) -> float:
    """Round an order size onto the asset's size grid."""
    return round(size, sz_decimals)


def round_price(price: float, sz_decimals: int) -> float:
    """Round a limit price onto HL's price grid (5 sig figs, then decimals)."""
    return round(float(f"{price:.{_PRICE_SIG_FIGS}g}"), _PERP_MAX_DECIMALS - sz_decimals)


@dataclass(frozen=True, slots=True)
class HLOpResult:
    """Outcome of one HL post-and-cancel round-trip."""

    intent_id: str
    status: Literal["confirmed", "failed", "dry_run"]
    duration_ms: float
    order_id: int | None
    fill_size: float  # should always be 0.0 — non-zero means a real fill leaked through


class HLTraceExecutor:
    """Places and cancels tiny post-only orders to measure HL local latency.

    Not thread-safe. Callers must serialize (README §11 I7).

    The `exchange_factory` is a callable that returns a hyperliquid.exchange.Exchange
    instance — injected so tests can supply a fake without importing the SDK.
    """

    def __init__(
        self,
        *,
        config: Config,
        store: StateStore,
        guard: MicroOpsGuard,
        exchange_factory: Any,
        get_mark_price: Any,
        get_size_decimals: Any = None,
        coin: str = "ETH",
    ) -> None:
        self._config = config
        self._store = store
        self._guard = guard
        self._make_exchange = exchange_factory
        self._get_mark_price = get_mark_price
        # Injected like `get_mark_price` so tests never import the SDK. When
        # absent, fall back to ETH's grid: wrong for an exotic asset, but the
        # tracer only ever quotes the coin it was constructed with, and a
        # mis-rounded order is refused locally rather than mispriced on-venue.
        self._get_size_decimals = get_size_decimals
        self._coin = coin
        self._exchange: Any = None

    def _exchange_once(self) -> Any:
        """Build the SDK client once and keep it for the executor's lifetime.

        `Exchange.__init__` constructs an `Info`, which pulls the perp and spot
        metadata over HTTP — two round trips. Rebuilding it per order put those
        inside the measured window and made the first live P1/P2 sample 2.6 s
        against a 2 s budget. Worse than the bad number: P1/P2 is the
        liquidation-response path, and standing up an HTTP client mid-emergency
        is the opposite of what that budget exists to protect.
        """
        if self._exchange is None:
            self._exchange = self._make_exchange()
        return self._exchange

    async def _size_decimals(self) -> int:
        if self._get_size_decimals is None:
            return _ETH_SZ_DECIMALS
        return int(await self._get_size_decimals(self._coin))

    async def post_and_cancel(self, side: Literal["buy", "sell"] = "sell") -> HLOpResult:
        """Place a post-only ALO order 10 % from mark, then cancel it.

        Returns the total round-trip duration measured with `latency.now_perf`.
        The whole sequence goes through the safety guard as a single
        `hl_post_only_cancel` op — one guard check, one journaled intent.
        """
        # Guard first — before any network activity.
        self._guard.check("hl_post_only_cancel", notional_usd=_MIN_NOTIONAL_USD)

        # Get a mark price (from stream cache preferably, else REST).
        mark = await self._get_mark_price(self._coin)
        sz_decimals = await self._size_decimals()
        offset = _POST_ONLY_OFFSET
        # For a sell post-only, the limit MUST be strictly above mark so it
        # rests as a maker. For a buy, strictly below. Rounding to HL's price
        # grid moves the limit by at most one tick — a 10 % offset absorbs that
        # without ever crossing back over mark.
        raw_limit = mark * (1.0 + offset) if side == "sell" else mark * (1.0 - offset)
        limit_price = round_price(raw_limit, sz_decimals)

        # Size chosen so notional ≈ _MIN_NOTIONAL_USD at mark price, then
        # snapped to the asset's size grid.
        size = round_size(_MIN_NOTIONAL_USD / mark, sz_decimals)
        notional = size * mark
        if notional < _HL_MIN_NOTIONAL_USD:
            # Rounding down took us under HL's floor: step one tick up rather
            # than send an order the venue will reject.
            size = round_size(size + 10.0**-sz_decimals, sz_decimals)
            notional = size * mark

        intent_id = deterministic_id(
            "hl_post_only_cancel",
            side,
            self._coin,
            f"{size:.9f}",
            f"{limit_price:.4f}",
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        await self._insert_pending_intent(
            intent_id,
            params={
                "coin": self._coin,
                "side": side,
                "size": size,
                "limit_price": limit_price,
                "mark_at_placement": mark,
            },
        )

        # Built before the clock starts, and only on the first live order: the
        # SDK client's construction is setup cost, not P1/P2 latency. Skipped
        # entirely in dry run, where the factory is deliberately a landmine.
        exchange = None if self._config.tracer.dry_run else self._exchange_once()

        start = now_perf()

        if self._config.tracer.dry_run:
            log.info(
                "hl_op_dry_run",
                message="hl_post_only_cancel: dry-run, aucun ordre envoyé",
                coin=self._coin,
                side=side,
                size=size,
                limit_price=limit_price,
            )
            duration_ms = elapsed_ms(start)
            await self._store.record_latency(
                measurement_path("path.p1_p2_hl_local", dry_run=True),
                duration_ms,
            )
            await self._mark_intent_status(intent_id, "confirmed", None)
            return HLOpResult(
                intent_id=intent_id,
                status="dry_run",
                duration_ms=duration_ms,
                order_id=None,
                fill_size=0.0,
            )

        try:
            is_buy = side == "buy"
            order_response = await self._call_exchange_order(
                exchange,
                self._coin,
                is_buy,
                size,
                limit_price,
            )
            # A rejected order comes back as an error envelope, not an
            # exception. Left unchecked, `_extract_order_id` simply returns
            # None, the cancel is skipped and the intent is journaled as
            # confirmed — putting a P1/P2 latency sample in the M1 report for
            # an order that was never placed.
            ensure_ok(order_response, "ordre post-only")
            order_id = self._extract_order_id(order_response)
            fill_size = self._extract_fill_size(order_response)
            if fill_size > 0:
                log.warning(
                    "hl_post_only_filled",
                    message=(
                        f"post-only sensé être maker s'est fait fill de "
                        f"{fill_size} — vérifier l'écart"
                    ),
                    fill_size=fill_size,
                    coin=self._coin,
                )
            # Cancel immediately.
            if order_id is not None:
                await self._call_exchange_cancel(exchange, self._coin, order_id)
        except Exception:
            await self._mark_intent_status(intent_id, "failed", None)
            log.exception(
                "hl_op_failed",
                message="hl_post_only_cancel: échec du round-trip",
                intent_id=intent_id,
            )
            raise

        duration_ms = elapsed_ms(start)
        await self._store.record_latency("path.p1_p2_hl_local", duration_ms)
        await self._mark_intent_status(intent_id, "confirmed", None)
        log.info(
            "hl_op_confirmed",
            message=f"hl_post_only_cancel round-trip {duration_ms:.1f} ms",
            duration_ms=duration_ms,
            order_id=order_id,
            fill_size=fill_size,
        )
        return HLOpResult(
            intent_id=intent_id,
            status="confirmed",
            duration_ms=duration_ms,
            order_id=order_id,
            fill_size=fill_size,
        )

    # --- SDK adapter methods (indirection for testability) --------------------

    async def _call_exchange_order(
        self,
        exchange: Any,
        coin: str,
        is_buy: bool,
        size: float,
        limit_price: float,
    ) -> dict[str, Any]:
        """Place a post-only ALO order via the HL SDK (sync -> await via thread)."""
        order_type = {"limit": {"tif": "Alo"}}
        return await asyncio.to_thread(
            exchange.order,
            coin,
            is_buy,
            size,
            limit_price,
            order_type,
        )

    async def _call_exchange_cancel(
        self,
        exchange: Any,
        coin: str,
        order_id: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(exchange.cancel, coin, order_id)

    @staticmethod
    def _extract_order_id(response: dict[str, Any]) -> int | None:
        # HL SDK response shape:
        #   {"status": "ok", "response": {"type": "order", "data": {"statuses": [...]}}}
        # Each status entry is either {"resting": {"oid": int}} or {"filled": {...}}.
        try:
            statuses = response["response"]["data"]["statuses"]
            for entry in statuses:
                if isinstance(entry, dict):
                    if "resting" in entry:
                        return int(entry["resting"]["oid"])
                    if "filled" in entry:
                        return int(entry["filled"]["oid"])
        except (KeyError, TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _extract_fill_size(response: dict[str, Any]) -> float:
        try:
            statuses = response["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return 0.0
        total = 0.0
        for entry in statuses:
            if isinstance(entry, dict) and "filled" in entry:
                try:
                    total += float(entry["filled"].get("totalSz", 0.0))
                except (TypeError, ValueError):
                    continue
        return total

    # --- Journal helpers ------------------------------------------------------

    async def _insert_pending_intent(
        self,
        intent_id: str,
        params: dict[str, object],
    ) -> None:
        assert self._store._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._store._conn.execute(
            """
            INSERT OR IGNORE INTO intents
                (id, created_at, action, priority, params_json, reason, status, updated_at)
            VALUES (?, ?, 'hl_post_only_cancel', 0, ?, 'micro-op M1-B2 HL', 'pending', ?)
            """,
            (intent_id, now, json.dumps(params, sort_keys=True, default=str), now),
        )
        await self._store._conn.commit()

    async def _mark_intent_status(
        self,
        intent_id: str,
        status: Literal["sent", "confirmed", "failed"],
        tx_hashes: list[str] | None,
    ) -> None:
        assert self._store._conn is not None
        await self._store._conn.execute(
            """
            UPDATE intents
               SET status = ?, tx_hashes = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                status,
                json.dumps(tx_hashes) if tx_hashes else None,
                datetime.now(UTC).isoformat(),
                intent_id,
            ),
        )
        await self._store._conn.commit()
