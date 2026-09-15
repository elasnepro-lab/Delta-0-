"""Operator alerts — routing, collapsing, and the guarantees that matter.

The four constraints of `delta0.alerts` are each pinned down here: it never
blocks, it never raises into the caller, it never floods, and it never goes
quiet without saying so.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from pydantic import SecretStr

from delta0.alerts import (
    CRITICAL,
    WARN,
    Alert,
    AlertSink,
    alert_from_event,
    build_sink,
    make_alert_processor,
    resolve_secret,
)
from delta0.config import Config


class _Recorder:
    """Transport that records what it was handed."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        self.texts.append(text)

    async def aclose(self) -> None:
        self.closed = True


class _Exploding:
    """Transport that always fails, like a revoked token would."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, text: str) -> None:
        self.attempts += 1
        raise RuntimeError("401 unauthorized")

    async def aclose(self) -> None:
        raise RuntimeError("still broken")


# --- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("warning", WARN),
        ("error", CRITICAL),
        ("critical", CRITICAL),
    ],
)
def test_levels_that_raise_the_alarm(level: str, expected: str) -> None:
    alert = alert_from_event({"level": level, "event": "boom", "message": "ça casse"})
    assert alert is not None
    assert alert.level == expected


@pytest.mark.parametrize("level", ["info", "debug", "", "notice"])
def test_levels_that_stay_quiet(level: str) -> None:
    """`log.info` must not reach the channel — see the module docstring.

    The daily digest of README §12 is a separate, deliberate message. Wiring
    every info line to Telegram would train the operator to mute it.
    """
    assert alert_from_event({"level": level, "event": "cycle_ok"}) is None


def test_an_event_can_opt_out() -> None:
    assert alert_from_event({"level": "error", "event": "noisy", "alert": False}) is None


def test_fields_are_carried_but_the_plumbing_is_dropped() -> None:
    alert = alert_from_event(
        {
            "level": "critical",
            "event": "reconcile_hf_low",
            "message": "HF sous le seuil",
            "timestamp": "2026-09-12T00:00:00Z",
            "hf": 1.02,
            "run_id": "abc123",
            "empty": None,
        },
    )
    assert alert is not None
    assert alert.fields == {"hf": 1.02, "run_id": "abc123"}


def test_render_puts_the_level_and_event_first() -> None:
    text = Alert(
        level=CRITICAL, event="p3_fired", message="repay urgent", fields={"hf": 1.1}
    ).render()
    assert text.splitlines()[0] == "[CRITICAL] p3_fired"
    assert "repay urgent" in text
    assert "hf=1.1" in text


def test_render_marks_a_collapsed_repeat() -> None:
    text = Alert(level=WARN, event="aave_supply_failed", message="", count=34).render()
    assert text.splitlines()[0] == "[WARN] aave_supply_failed x34"


def test_a_traceback_is_reduced_to_its_cause() -> None:
    """A phone notification is not the place for a stack trace.

    `log.exception` attaches the whole traceback. Pasted into the fields line
    it turned the alert into a wall of text; the last line is the one that
    names the failure, and the frames stay in the journal.
    """
    alert = alert_from_event(
        {
            "level": "error",
            "event": "op_send_failed",
            "message": "envoi en échec",
            "exception": "\n".join(
                [
                    "Traceback (most recent call last):",
                    '  File "x.py", line 3, in main',
                    "    raise ValueError('boom')",
                    "ValueError: boom",
                ],
            ),
        },
    )
    assert alert is not None
    assert alert.cause == "ValueError: boom"
    assert "exception" not in alert.fields
    rendered = alert.render()
    assert "ValueError: boom" in rendered
    assert "Traceback" not in rendered


def test_an_event_without_a_traceback_has_no_cause() -> None:
    alert = alert_from_event({"level": "warning", "event": "lent"})
    assert alert is not None
    assert alert.cause is None


def test_render_is_truncated_for_telegram() -> None:
    text = Alert(level=WARN, event="e", message="x" * 9_000).render()
    assert len(text) <= 3_500


# --- collapsing --------------------------------------------------------------


def _alert(event: str = "boom", level: str = WARN) -> Alert:
    return Alert(level=level, event=event, message="m")


@pytest.mark.asyncio
async def test_the_first_of_a_kind_leaves_immediately(monkeypatch: Any) -> None:
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=900.0)
    sink.emit(_alert())
    await sink.stop()
    assert len(recorder.texts) == 1


@pytest.mark.asyncio
async def test_repeats_inside_the_window_collapse_into_one_summary() -> None:
    """The shape of the 8 September incident: one cause, 34 repetitions.

    One alert when it starts, one summary when the window closes. Not 35
    messages.
    """
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=900.0)
    for _ in range(35):
        sink.emit(_alert("aave_supply_failed"))
    await sink.stop()
    assert len(recorder.texts) == 2
    assert "x34" in recorder.texts[1]


@pytest.mark.asyncio
async def test_the_summary_keeps_the_cause() -> None:
    """An operator who only reads the summary must still see what failed."""
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=900.0)
    for _ in range(3):
        sink.emit(
            Alert(
                level=CRITICAL,
                event="op_send_failed",
                message="aave_supply en échec",
                cause="ValueError: ERC20: transfer amount exceeds balance",
            ),
        )
    await sink.stop()
    assert "exceeds balance" in recorder.texts[-1]


@pytest.mark.asyncio
async def test_a_single_occurrence_gets_no_redundant_summary() -> None:
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=900.0)
    sink.emit(_alert())
    await sink.stop()
    assert len(recorder.texts) == 1


@pytest.mark.asyncio
async def test_distinct_events_do_not_collapse_into_each_other() -> None:
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=900.0)
    sink.emit(_alert("repay_failed"))
    sink.emit(_alert("withdraw_failed"))
    sink.emit(_alert("repay_failed", level=CRITICAL))
    await sink.stop()
    assert len(recorder.texts) == 3


@pytest.mark.asyncio
async def test_a_new_window_opens_once_the_old_one_elapsed() -> None:
    recorder = _Recorder()
    sink = AlertSink(recorder, collapse_window_s=0.0)
    sink.emit(_alert())
    sink.emit(_alert())
    await sink.stop()
    # Window length zero: every occurrence is a first occurrence.
    assert len(recorder.texts) == 2


# --- the guarantees ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_channel_never_raises_into_the_caller() -> None:
    """A revoked token must not kill a decision cycle."""
    transport = _Exploding()
    sink = AlertSink(transport)
    sink.emit(_alert())
    await sink.stop()  # must not raise, including on aclose
    assert transport.attempts == 1


@pytest.mark.asyncio
async def test_a_saturated_buffer_says_how_much_it_lost() -> None:
    """Losing alerts is acceptable. Losing them silently is not."""
    recorder = _Recorder()
    sink = AlertSink(recorder, max_buffered=3, collapse_window_s=0.0)
    for i in range(10):
        sink.emit(_alert(f"e{i}"))
    assert sink.dropped == 7
    await sink.stop()
    overflow = [t for t in recorder.texts if "alert_buffer_overflow" in t]
    assert len(overflow) == 1
    assert "7 alertes perdues" in overflow[0]


def test_emit_does_not_block_and_needs_no_event_loop() -> None:
    """`emit` is called from the decision path, which cannot wait on a POST."""
    sink = AlertSink(_Recorder(), collapse_window_s=0.0)
    start = time.perf_counter()
    for i in range(2_000):
        sink.emit(_alert(f"e{i}"))
    assert (time.perf_counter() - start) < 0.5


def test_emit_is_safe_from_worker_threads() -> None:
    """The Hyperliquid SDK is synchronous and logs from `asyncio.to_thread`."""
    sink = AlertSink(_Recorder(), max_buffered=10_000, collapse_window_s=0.0)

    def hammer(offset: int) -> None:
        for i in range(200):
            sink.emit(_alert(f"e{offset}-{i}"))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sink.dropped == 0


@pytest.mark.asyncio
async def test_the_drain_task_sends_without_being_asked() -> None:
    recorder = _Recorder()
    sink = AlertSink(recorder, poll_s=0.01, collapse_window_s=0.0)
    await sink.start()
    sink.emit(_alert())
    await asyncio.sleep(0.1)
    assert len(recorder.texts) >= 1
    await sink.stop()
    assert recorder.closed


# --- the processor -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_processor_passes_the_event_through_untouched() -> None:
    sink = AlertSink(_Recorder(), collapse_window_s=0.0)
    processor = make_alert_processor(sink)
    event = {"level": "error", "event": "boom", "message": "m"}
    assert processor(None, "", dict(event)) == event


@pytest.mark.asyncio
async def test_an_alerting_failure_cannot_alert_about_itself() -> None:
    """Without the re-entrancy guard this is an infinite loop.

    The send path logs a warning when it fails, and warnings are alerts, so a
    broken channel would generate one alert per failed alert, forever.
    """
    transport = _Exploding()
    sink = AlertSink(transport, poll_s=0.01, collapse_window_s=0.0)
    # The sink's own failure log goes through the real processor chain.
    make_alert_processor(sink)
    await sink.start()
    sink.emit(_alert())
    await asyncio.sleep(0.15)
    await sink.stop()
    # One emitted alert, retried never: a handful of attempts at most, not
    # thousands.
    assert transport.attempts <= 3


# --- secret resolution -------------------------------------------------------


class _FakeSettings:
    tg_token = SecretStr("secret-token")
    tg_chat = "12345"


def test_env_indirection_is_resolved() -> None:
    settings: Any = _FakeSettings()
    assert resolve_secret("env:TG_TOKEN", settings) == "secret-token"
    assert resolve_secret("env:TG_CHAT", settings) == "12345"


def test_a_literal_value_is_taken_as_is() -> None:
    settings: Any = _FakeSettings()
    assert resolve_secret("123:abc", settings) == "123:abc"


def test_an_unknown_env_name_resolves_to_empty_not_a_crash() -> None:
    settings: Any = _FakeSettings()
    assert resolve_secret("env:NOT_A_SETTING", settings) == ""


# --- armed or not, but never in doubt ----------------------------------------


@pytest.mark.asyncio
async def test_no_credentials_means_no_sink_rather_than_a_silent_one(
    config: Config,
) -> None:
    """None, not a sink that discards.

    A half-wired alert path that swallows everything is the illusion this
    chantier exists to remove — the same shape as `ARBITRUM_RPC_FALLBACK`,
    declared and read by nobody. Returning None forces the caller to say out
    loud whether the filet is armed.
    """

    class _Empty:
        tg_token = SecretStr("")
        tg_chat = ""

    assert build_sink(config, _Empty()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_missing_chat_id_is_as_disabling_as_a_missing_token(
    config: Config,
) -> None:
    class _HalfWired:
        tg_token = SecretStr("a-token")
        tg_chat = ""

    assert build_sink(config, _HalfWired()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_both_credentials_present_arms_the_sink(config: Config) -> None:
    sink = build_sink(config, _FakeSettings())  # type: ignore[arg-type]
    assert sink is not None
    await sink.stop()
