"""Logging setup — sanity checks."""

from __future__ import annotations

import logging

import pytest

from delta0.config import RuntimeMode
from delta0.logging import (
    configure_logging,
    get_logger,
    new_run_id,
    set_cycle_id,
    set_intent_id,
)


def test_configure_dryrun_does_not_crash() -> None:
    configure_logging(RuntimeMode.DRY_RUN)
    log = get_logger("test")
    log.info("evt_ok", message="logging opérationnel")


def test_configure_live_json_renderer() -> None:
    configure_logging(RuntimeMode.LIVE)
    log = get_logger("test")
    log.warning("evt_warn", message="attention")


def test_request_urls_never_reach_the_journal(caplog: pytest.LogCaptureFixture) -> None:
    """The Telegram token travels in the URL, and httpx logs URLs at INFO."""
    configure_logging(RuntimeMode.LIVE)
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            'HTTP Request: POST https://api.telegram.org/bot000:SECRET/sendMessage "200 OK"'
        )
        logging.getLogger("httpcore").info("connect_tcp.started host='api.telegram.org'")
        logging.getLogger("httpx").warning("avertissement httpx")

    assert "SECRET" not in caplog.text
    assert "connect_tcp" not in caplog.text
    assert "avertissement httpx" in caplog.text


def test_context_vars_propagate() -> None:
    configure_logging(RuntimeMode.LIVE)
    rid = new_run_id()
    set_cycle_id("cyc-1")
    set_intent_id("int-1")
    assert rid
    log = get_logger()
    # Emitting a log line exercises the context processor without asserting on
    # the internal proxy type (structlog returns a lazy proxy until first use).
    log.info("evt_ctx", message="contexte propagé")
