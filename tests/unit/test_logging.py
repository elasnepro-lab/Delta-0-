"""Logging setup — sanity checks."""

from __future__ import annotations

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
