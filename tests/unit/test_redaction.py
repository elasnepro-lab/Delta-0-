"""Secrets carried in URLs never reach a log line, an alert or the journal.

Found in the end-of-session review of 2026-09-14: when both RPC endpoints answer
a 429 or a 5xx, aiohttp's error prints the full URL — Alchemy's API key is its
path — and the watcher logged it into a field the alert processor forwards to
the Telegram group.
"""

from __future__ import annotations

from typing import Any

import pytest

from delta0 import failure
from delta0.alerts import alert_from_event
from delta0.redaction import redact_event, redact_secrets

ALCHEMY = "https://arb-mainnet.g.alchemy.com/v2/SECRETKEY123"
TELEGRAM = "https://api.telegram.org/bot000:SECRETTOKEN/sendMessage"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (ALCHEMY, "https://arb-mainnet.g.alchemy.com/…"),
        (TELEGRAM, "https://api.telegram.org/…"),
        ("wss://user:SECRETPASS@node.example:8546/ws", "wss://node.example:8546/…"),
        ("https://api.hyperliquid.xyz", "https://api.hyperliquid.xyz"),
    ],
)
def test_a_url_keeps_its_host_and_loses_its_secrets(text: str, kept: str) -> None:
    redacted = redact_secrets(text)
    assert redacted == kept
    assert "SECRET" not in redacted


def test_the_aiohttp_error_text_is_redacted_inside_a_repr() -> None:
    """The exact shape: `url='...'` inside ClientResponseError's message."""
    message = f"ClientResponseError: 429, message='Too Many Requests', url='{ALCHEMY}'"
    redacted = redact_secrets(message)
    assert "SECRETKEY123" not in redacted
    assert "arb-mainnet.g.alchemy.com" in redacted
    assert "429" in redacted


def test_the_processor_redacts_every_string_field_before_alerts_see_it() -> None:
    event: dict[str, Any] = {
        "level": "error",
        "event": "aave_read_failed",
        "message": "lecture Aave en échec",
        "error": f"ClientResponseError(url='{ALCHEMY}')",
        "exception": f"Traceback...\naiohttp.ClientResponseError: 503, url='{ALCHEMY}'",
        "attempt": 3,
    }
    alert = alert_from_event(redact_event(None, "error", event))
    assert alert is not None
    assert "SECRETKEY123" not in alert.render()
    assert event["attempt"] == 3


def test_the_journal_cause_is_redacted() -> None:
    cause = failure.from_exception(RuntimeError(f"503, url='{ALCHEMY}'"))
    assert "SECRETKEY123" not in cause.journal_entry()
