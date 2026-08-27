"""Hyperliquid WebSocket stream — async-friendly wrapper around the SDK.

The official HL SDK uses a background thread for its WS; we shim it to
asyncio queues so the rest of the bot stays cleanly async.

Subscriptions exposed:
- `mark_ticks`     : `allMids` — one entry per known coin per update, we filter
                     for the configured coin and push floats to a queue.
- `user_events`    : `userEvents` — fills, funding, liquidations for our address.

Both queues are bounded: on overflow, the OLDEST value is dropped. Fresh data
matters, stale data does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from hyperliquid.info import Info

from delta0.logging import get_logger

log = get_logger(__name__)

_QUEUE_MAX = 256


@dataclass(slots=True)
class MarkTick:
    coin: str
    mark_price: float
    ts_monotonic: float


@dataclass(slots=True)
class UserEvent:
    kind: str  # "fill" | "funding" | "liquidation" | "unknown"
    raw: dict[str, Any]
    ts_monotonic: float


@dataclass(slots=True)
class HyperliquidStream:
    """Async wrapper around the SDK's WS subscriptions.

    Not a context manager on purpose: `start()` and `stop()` are called
    from the tracer's `run()` and its finally block. The SDK's WS thread
    outlives individual coroutines, so we own its lifecycle explicitly.
    """

    api_url: str
    user_address: str
    coin: str = "ETH"

    _info: Info | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _mark_queue: asyncio.Queue[MarkTick] = field(default_factory=lambda: asyncio.Queue(_QUEUE_MAX))
    _events_queue: asyncio.Queue[UserEvent] = field(
        default_factory=lambda: asyncio.Queue(_QUEUE_MAX),
    )

    def start(self) -> None:
        if self._info is not None:
            return
        self._loop = asyncio.get_running_loop()
        # skip_ws=False starts the WS thread.
        self._info = Info(self.api_url, skip_ws=False)
        # Mark price subscription: `allMids` gives {coin: str_price}.
        self._info.subscribe({"type": "allMids"}, self._on_mids)
        # User events subscription: fills + liquidations + funding for our address.
        self._info.subscribe(
            {"type": "userEvents", "user": self.user_address},
            self._on_user_event,
        )
        log.info("hl_stream_started", message=f"WS Hyperliquid démarré sur {self.coin}")

    def stop(self) -> None:
        # SDK exposes .disconnect on the ws manager; call it if available.
        if self._info is not None:
            ws_manager = getattr(self._info, "ws_manager", None)
            if ws_manager is not None and hasattr(ws_manager, "stop"):
                ws_manager.stop()
        self._info = None
        log.info("hl_stream_stopped", message="WS Hyperliquid arrêté")

    # --- Public queues --------------------------------------------------------

    async def next_mark_tick(self, timeout_s: float | None = None) -> MarkTick | None:
        try:
            return await asyncio.wait_for(self._mark_queue.get(), timeout=timeout_s)
        except TimeoutError:
            return None

    async def next_user_event(self, timeout_s: float | None = None) -> UserEvent | None:
        try:
            return await asyncio.wait_for(self._events_queue.get(), timeout=timeout_s)
        except TimeoutError:
            return None

    def try_latest_mark(self) -> MarkTick | None:
        """Non-blocking : drain the queue and return the latest tick, if any."""
        latest: MarkTick | None = None
        while True:
            try:
                latest = self._mark_queue.get_nowait()
            except asyncio.QueueEmpty:
                return latest

    def drain_user_events(self) -> list[UserEvent]:
        """Non-blocking : drain all pending user events."""
        events: list[UserEvent] = []
        while True:
            try:
                events.append(self._events_queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    # --- SDK callbacks (called from the WS thread) ----------------------------

    def _on_mids(self, msg: dict[str, Any]) -> None:
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        mids = data.get("mids") if "mids" in data else data
        if not isinstance(mids, dict):
            return
        raw = mids.get(self.coin)
        if raw is None:
            return
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return
        tick = MarkTick(coin=self.coin, mark_price=price, ts_monotonic=_monotonic())
        self._enqueue(self._mark_queue, tick)

    def _on_user_event(self, msg: dict[str, Any]) -> None:
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        kind = "unknown"
        if "fills" in data:
            kind = "fill"
        elif "funding" in data:
            kind = "funding"
        elif "liquidation" in data:
            kind = "liquidation"
        event = UserEvent(kind=kind, raw=data, ts_monotonic=_monotonic())
        self._enqueue(self._events_queue, event)

    def _enqueue(self, queue: asyncio.Queue[Any], item: Any) -> None:
        """Enqueue an item onto the asyncio loop.

        When called from the SDK's WS thread, we cross the thread boundary via
        `call_soon_threadsafe`. When called from within the loop (e.g., unit
        tests exercising the callback directly), we can put synchronously.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _drop_oldest_and_put(queue, item)
        else:
            loop.call_soon_threadsafe(_drop_oldest_and_put, queue, item)


def _drop_oldest_and_put(queue: asyncio.Queue[Any], item: Any) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)


def _monotonic() -> float:
    return time.monotonic()
