"""Chantier 6.4 — the loop survives failures, never bugs.

Thirteen `except Exception` used to catch everything alike: an `AttributeError`
from a typo was logged as "le cycle a levé" and the tracer carried on, a bug
reading as a network hiccup. These tests hold the line from three sides: what
counts as a survivable failure, what does not, and that no new blind `except`
can swallow silently without saying why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import aiohttp
import pytest
import requests
from eth_abi.exceptions import InsufficientDataBytes
from hyperliquid.utils.error import ServerError
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

from delta0.errors import Delta0Error, VenueError
from delta0.failure import OPERATIONAL_ERRORS
from delta0.hl_client import HLActionRefused
from delta0.safety import InsufficientBalance, SafetyRefused
from delta0.venues.aave import MulticallError

SRC = Path(__file__).resolve().parents[2] / "src" / "delta0"

# Handlers allowed to catch everything WITHOUT re-raising, by (file, function).
SWALLOW_ALLOWED: dict[tuple[str, str], str] = {
    ("alerts.py", "stop"): "fermer le canal d'alerte ne doit jamais faire échouer l'arrêt du bot",
    ("alerts.py", "_send_one"): (
        "une alerte en échec ne peut ni lever chez l'appelant ni s'alerter elle-même"
    ),
}


# --- What the loop may survive ----------------------------------------------------


def test_the_bot_s_own_errors_share_one_root() -> None:
    for cls in (SafetyRefused, InsufficientBalance, HLActionRefused, MulticallError):
        assert issubclass(cls, Delta0Error), cls
    for cls in (HLActionRefused, MulticallError):
        assert issubclass(cls, VenueError), cls


@pytest.mark.parametrize(
    "cls",
    [
        TimeoutError,
        ConnectionResetError,
        requests.ConnectionError,  # the Hyperliquid SDK's transport
        aiohttp.ClientConnectionError,  # web3's async transport
        ContractLogicError,  # a revert
        TimeExhausted,  # a receipt that never came
        Web3RPCError,
        InsufficientDataBytes,  # a contract answered bytes that do not decode
        ServerError,  # Hyperliquid HTTP 5xx
        SafetyRefused,
        InsufficientBalance,
        HLActionRefused,
        MulticallError,
    ],
)
def test_operational_failures_are_survivable(cls: type[BaseException]) -> None:
    assert issubclass(cls, OPERATIONAL_ERRORS)


@pytest.mark.parametrize(
    "cls",
    [
        AttributeError,
        NameError,
        TypeError,
        KeyError,
        IndexError,
        AssertionError,
        ZeroDivisionError,
        NotImplementedError,
        # A local refusal such as the SDK's `float_to_wire causes rounding` is a
        # bug in our own rounding, not a venue being unavailable.
        ValueError,
    ],
)
def test_programming_errors_are_not(cls: type[BaseException]) -> None:
    assert not issubclass(cls, OPERATIONAL_ERRORS)


# --- No new blind except ----------------------------------------------------------


def _is_blind(handler: ast.ExceptHandler) -> bool:
    return handler.type is None or (
        isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}
    )


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Raise) and node.exc is None
        for statement in handler.body
        for node in ast.walk(statement)
    )


class _BlindHandlers(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.stack: list[str] = []
        self.found: list[tuple[str, str, ast.ExceptHandler]] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if _is_blind(node):
            where = self.stack[-1] if self.stack else "<module>"
            self.found.append((self.filename, where, node))
        self.generic_visit(node)


def _swallowing_handlers() -> list[tuple[str, str]]:
    swallowing: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        finder = _BlindHandlers(path.name)
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        swallowing.extend((name, func) for name, func, h in finder.found if not _reraises(h))
    return swallowing


def test_a_blind_except_reraises_or_says_why_it_may_swallow() -> None:
    offenders = sorted(set(_swallowing_handlers()) - SWALLOW_ALLOWED.keys())
    assert not offenders, (
        f"`except Exception` qui avale sans relancer : {offenders}. Attrapez "
        "failure.OPERATIONAL_ERRORS ou une exception précise, relancez, ou justifiez "
        "l'exception dans SWALLOW_ALLOWED."
    )


def test_the_swallow_allowlist_holds_no_stale_entry() -> None:
    stale = sorted(SWALLOW_ALLOWED.keys() - set(_swallowing_handlers()))
    assert not stale, f"SWALLOW_ALLOWED nomme des handlers qui n'avalent plus rien : {stale}"
