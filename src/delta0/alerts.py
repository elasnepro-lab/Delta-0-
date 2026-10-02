"""Operator alerts — the filet that was missing during the marche à blanc.

The M1 campaign recorded 128 failed intents over 60 hours and told nobody. The
events existed, the journal was written, the loop was alive: there was nothing
left to detect, only to deliver. This module delivers.

Four design constraints, each of them learned rather than assumed.

**It never blocks the loop.** A Telegram round trip costs ~200 ms, and P1/P2
has a 2 s budget. `AlertSink.emit` only appends to a bounded buffer; a
background task drains it. An alert backlog must never become backpressure on
a trading loop.

**It never raises into the caller.** Every transport error is swallowed and
logged locally. An alerting failure that kills a decision cycle would be a
worse bug than the one it was reporting.

**It never floods.** Identical events collapse: the first one leaves
immediately, repeats inside the window are counted, and the window closes with
one summary. The incident of 8 September would have produced one alert at
20h39 and then a handful of "aave_supply x34" lines, instead of 86 messages
teaching the operator to mute the channel.

**And it goes quiet on its own when nothing changes.** A fixed window only
solves the burst. Alchemy refused every `eth_call` from 28 to 30 September and
the fault never cleared: 42 hours of 15-minute windows, about 340 messages for
one incident the operator understood from the first. So each window that closes
on a condition STILL firing is followed by a longer one, doubling up to six
hours — the same 42 hours now cost about a dozen messages. A condition that
goes quiet for one full window retires its key, so the next occurrence is a
fresh incident again: immediate, and back to the base window. Escalating the
silence is safe precisely because de-escalation is automatic.

**It is fed from any thread.** The Hyperliquid SDK is synchronous and runs in
worker threads, which log too, so the buffer is guarded by a plain lock rather
than being an `asyncio.Queue`.

Levels follow README §12, minus one. WARN and CRITICAL are emitted here. INFO
is the daily digest, which *is* the exactitude table, and that table needs
accounting dimensions the bot does not compute yet. Wiring `log.info` to the
channel meanwhile would flood it with cycle noise and train the operator to
ignore the one message that matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from delta0.config import Config
from delta0.logging import get_logger
from delta0.settings import Settings

log = get_logger(__name__)

WARN = "WARN"
CRITICAL = "CRITICAL"

# structlog level name -> alert level. `log.exception` arrives as "error".
# `info` and `debug` are deliberately absent (see the module docstring).
_LEVEL_MAP: dict[str, str] = {
    "warning": WARN,
    "error": CRITICAL,
    "critical": CRITICAL,
}

# Fields that carry no information for a human reading a phone at 3 a.m.
_BORING_FIELDS = frozenset(
    {"level", "timestamp", "event", "message", "alert", "exception"},
)

# Telegram refuses messages above 4096 characters.
_MAX_MESSAGE = 3_500

# The first window on a fresh incident. Short: a burst deserves a fast second
# opinion telling the operator whether it was one glitch or a fault.
DEFAULT_COLLAPSE_WINDOW_S = 900.0

# Growth applied to the next window each time one closes on a condition that
# is still firing, and the ceiling it stops at. Six hours means a permanent
# fault costs four messages a day instead of ninety-six.
DEFAULT_COLLAPSE_FACTOR = 2.0
DEFAULT_COLLAPSE_MAX_S = 21_600.0

# Set while the sink is formatting or sending, so that an error raised by the
# alerting path cannot produce an alert about itself, forever.
_in_alert: ContextVar[bool] = ContextVar("in_alert", default=False)


class AlertTransport(Protocol):
    """Where an alert goes. One implementation per channel."""

    async def send(self, text: str) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert, ready to be rendered."""

    level: str
    event: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)
    count: int = 1
    cause: str | None = None

    def render(self) -> str:
        """Plain text, most important first, no markup to escape."""
        head = f"[{self.level}] {self.event}"
        if self.count > 1:
            head += f" x{self.count}"
        lines = [head]
        if self.message:
            lines.append(self.message)
        if self.cause:
            lines.append(self.cause)
        extras = [f"{k}={v}" for k, v in sorted(self.fields.items())]
        if extras:
            lines.append(" ".join(extras))
        text = "\n".join(lines)
        return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 1] + "…"


@dataclass
class _Window:
    """Collapse state for one (level, event) key.

    `pending` counts the occurrences NOT yet reported, which keeps the
    arithmetic identical for the first window — where one occurrence left
    immediately — and for the ones that follow, where none did. `span_s` is
    this window's own length, so escalation is carried by the window rather
    than by a side table keyed on the same tuple.
    """

    opened_at: float
    span_s: float
    pending: int
    last: Alert


class AlertSink:
    """Buffers alerts and drains them in the background.

    `emit` is synchronous, thread-safe and non-blocking by contract. Start the
    drain task once at boot and stop it on the way out; `stop` flushes the
    summaries still held by open collapse windows, because the most useful
    moment to learn that something failed 34 times is not after the next
    restart.
    """

    def __init__(
        self,
        transport: AlertTransport,
        *,
        max_buffered: int = 256,
        collapse_window_s: float = DEFAULT_COLLAPSE_WINDOW_S,
        collapse_factor: float = DEFAULT_COLLAPSE_FACTOR,
        collapse_max_s: float = DEFAULT_COLLAPSE_MAX_S,
        poll_s: float = 1.0,
    ) -> None:
        self._transport = transport
        self._max_buffered = max_buffered
        self._collapse_window_s = collapse_window_s
        self._collapse_factor = collapse_factor
        self._collapse_max_s = collapse_max_s
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._buffer: list[Alert] = []
        self._windows: dict[tuple[str, str], _Window] = {}
        self._dropped = 0
        self._sent = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def dropped(self) -> int:
        """Alerts discarded because the buffer was full. Never silent."""
        return self._dropped

    @property
    def sent(self) -> int:
        return self._sent

    # --- producer side -------------------------------------------------------

    def emit(self, alert: Alert) -> None:
        """Buffer one alert. Safe from any thread, never blocks, never raises."""
        key = (alert.level, alert.event)
        now = time.monotonic()
        with self._lock:
            window = self._windows.get(key)
            if window is not None:
                if now - window.opened_at < window.span_s:
                    window.pending += 1
                    window.last = alert
                    return
                # Elapsed, and the drain task has not swept it yet. Close it
                # here rather than overwriting it: the old code replaced the
                # window outright, which silently dropped the summary it owed.
                if self._sweep(key, window, now):
                    window.pending = 1
                    window.last = alert
                    return
            self._windows[key] = _Window(
                opened_at=now,
                span_s=self._collapse_window_s,
                pending=0,
                last=alert,
            )
            self._append(alert)

    def _append(self, alert: Alert) -> None:
        """Caller holds the lock. Drops the NEWEST when full, and counts it.

        Dropping the newest rather than the oldest is deliberate: under a flood
        the first alerts are the ones that name the root cause, and the tail is
        repetition the collapse window is already summarising.
        """
        if len(self._buffer) >= self._max_buffered:
            self._dropped += 1
            return
        self._buffer.append(alert)

    # --- consumer side -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_forever(), name="alert-drain")

    async def stop(self) -> None:
        """Cancel the drain task, then flush what is still buffered."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._close_windows(force=True)
        await self._flush()
        try:
            await self._transport.aclose()
        except Exception:  # broad on purpose: closing alerts must never fail the bot's shutdown
            log.warning("alert_transport_close_failed", message="fermeture du canal en échec")

    async def _drain_forever(self) -> None:
        while True:
            await asyncio.sleep(self._poll_s)
            self._close_windows(force=False)
            await self._flush()

    def _sweep(self, key: tuple[str, str], window: _Window, now: float) -> bool:
        """Close one elapsed window. Caller holds the lock.

        Returns True when the key stays armed, because the condition is still
        firing and the next window opens longer. False when it stayed quiet
        for a whole window, which retires the key: the next occurrence is then
        a fresh incident, alerted immediately and back at the base span.
        """
        if window.pending == 0:
            del self._windows[key]
            return False
        self._append(
            Alert(
                level=window.last.level,
                event=window.last.event,
                message=window.last.message,
                fields=window.last.fields,
                count=window.pending,
                cause=window.last.cause,
            ),
        )
        window.opened_at = now
        window.span_s = min(window.span_s * self._collapse_factor, self._collapse_max_s)
        window.pending = 0
        return True

    def _close_windows(self, *, force: bool) -> None:
        """Turn elapsed collapse windows into one summary alert each."""
        now = time.monotonic()
        with self._lock:
            for key, window in list(self._windows.items()):
                if not force and now - window.opened_at < window.span_s:
                    continue
                # On the way out nothing will reopen: sweep for the summary,
                # then retire the key whatever it wanted.
                if self._sweep(key, window, now) and force:
                    del self._windows[key]

    async def _flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
            dropped, self._dropped = self._dropped, 0
        if dropped:
            batch.append(
                Alert(
                    level=CRITICAL,
                    event="alert_buffer_overflow",
                    message=f"{dropped} alertes perdues — tampon saturé",
                ),
            )
        for alert in batch:
            await self._send_one(alert)

    async def _send_one(self, alert: Alert) -> None:
        token = _in_alert.set(True)
        try:
            await self._transport.send(alert.render())
            self._sent += 1
        except Exception:
            # Deliberately not `log.exception`: an alerting failure must not
            # queue an alert about itself. The re-entrancy guard above already
            # blocks that, and this keeps the local log readable too.
            log.warning(
                "alert_send_failed",
                message=f"envoi d'alerte en échec ({alert.event})",
                alert=False,
            )
        finally:
            _in_alert.reset(token)


class TelegramTransport:
    """Telegram Bot API. The token is never logged, not even truncated."""

    def __init__(self, token: str, chat_id: str, *, timeout_s: float = 10.0) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def send(self, text: str) -> None:
        response = await self._client.post(
            self._url,
            json={"chat_id": self._chat_id, "text": text, "disable_notification": False},
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def _last_frame(traceback: object) -> str | None:
    """The exception line of a traceback, without the frames above it.

    `log.exception` attaches the full traceback, which belongs in the journal
    and not in a phone notification: pasted into the fields line it turned the
    whole alert into a wall of text. The last non-empty line carries the type
    and the message, which is what names the failure.
    """
    if not isinstance(traceback, str):
        return None
    lines = [line.strip() for line in traceback.splitlines() if line.strip()]
    return lines[-1] if lines else None


def alert_from_event(event_dict: MutableMapping[str, Any]) -> Alert | None:
    """Turn one structlog event into an alert, or None when it is not one.

    Pure, so the routing rules are testable without a sink or a network.
    """
    level = str(event_dict.get("level", ""))
    alert_level = _LEVEL_MAP.get(level)
    if alert_level is None:
        return None
    # Explicit escape hatch for an event that proves noisy in production.
    if event_dict.get("alert") is False:
        return None
    fields = {k: v for k, v in event_dict.items() if k not in _BORING_FIELDS and v is not None}
    return Alert(
        level=alert_level,
        event=str(event_dict.get("event", "?")),
        message=str(event_dict.get("message", "")),
        fields=fields,
        cause=_last_frame(event_dict.get("exception")),
    )


def make_alert_processor(sink: AlertSink) -> Any:
    """structlog processor feeding `sink`, to sit just before the renderer.

    A processor rather than calls scattered through the loop and the executors:
    phases 3 and 4 are going to restructure both, and a sink wired at the
    logging layer survives that. It also means no call site can forget to
    alert.
    """

    def processor(
        _logger: Any,
        _name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        if _in_alert.get():
            return event_dict
        alert = alert_from_event(event_dict)
        if alert is not None:
            sink.emit(alert)
        return event_dict

    return processor


def resolve_secret(spec: str, settings: Settings) -> str:
    """Resolve a config value that may point at an environment variable.

    `AlertsConfig` ships `"env:TG_TOKEN"` by convention: the secret lives in
    the environment, never in the file that gets committed. Anything else is
    taken literally, which leaves a test or a fork free to pass a value
    inline. Until now that convention was declared and read by nobody — the
    third ghost setting found during M1, after `ARBITRUM_RPC_FALLBACK` and
    these two.
    """
    prefix = "env:"
    if not spec.startswith(prefix):
        return spec
    value = getattr(settings, spec[len(prefix) :].lower(), "")
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)


def build_sink(config: Config, settings: Settings) -> AlertSink | None:
    """The sink, or None when the channel is not configured.

    Returning None rather than a silently discarding sink is the point: the
    caller has to say out loud whether the filet is armed. A half-wired alert
    path that swallows everything is exactly the illusion this chantier exists
    to remove.
    """
    token = resolve_secret(config.alerts.telegram_bot_token, settings)
    chat = resolve_secret(config.alerts.telegram_chat_id, settings)
    if not token or not chat:
        return None
    return AlertSink(
        TelegramTransport(token, chat),
        collapse_window_s=config.alerts.collapse_window_s,
        collapse_factor=config.alerts.collapse_factor,
        collapse_max_s=config.alerts.collapse_max_s,
    )
