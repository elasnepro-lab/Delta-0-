"""Structured logging setup.

Rules (README section 12):
- JSON lines to stdout in LIVE / LIVE_SMALL; pretty console in DRY_RUN.
- Every log line carries a run_id (bot process) and, when relevant, a cycle_id
  (one decision cycle) and an intent_id (one action being executed).
- Event messages are written in French — logs and alerts are operator-facing.
- Field names and code identifiers stay in English.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any, cast

import structlog

from delta0.config import RuntimeMode

_run_id_var: ContextVar[str] = ContextVar("run_id", default="")
_cycle_id_var: ContextVar[str] = ContextVar("cycle_id", default="")
_intent_id_var: ContextVar[str] = ContextVar("intent_id", default="")


def _add_context(
    _logger: Any,
    _name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    run_id = _run_id_var.get()
    cycle_id = _cycle_id_var.get()
    intent_id = _intent_id_var.get()
    if run_id:
        event_dict["run_id"] = run_id
    if cycle_id:
        event_dict["cycle_id"] = cycle_id
    if intent_id:
        event_dict["intent_id"] = intent_id
    return event_dict


def new_run_id() -> str:
    rid = uuid.uuid4().hex[:12]
    _run_id_var.set(rid)
    return rid


def set_cycle_id(cycle_id: str) -> None:
    _cycle_id_var.set(cycle_id)


def set_intent_id(intent_id: str) -> None:
    _intent_id_var.set(intent_id)


def configure_logging(mode: RuntimeMode, level: str = "INFO") -> None:
    """Wire structlog once at startup. Idempotent."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if mode is RuntimeMode.DRY_RUN:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
