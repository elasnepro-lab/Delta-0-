"""The one door to the Hyperliquid SDK, and the two ways the SDK says no.

Every helper here exists because of a live incident. A spot-to-perp transfer
Hyperliquid declined — `{'status': 'err', ...}` — was logged as done because
the code only watched for exceptions. And on 2026-09-08 the testnet showed the
second way: a refusal nested inside `status: ok`, which the first check let
through. The envelopes come verbatim from those captures (`tests/hl_envelopes.py`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from delta0.hl_client import (
    HL_HTTP_TIMEOUT_S,
    HLActionRefused,
    ensure_ok,
    is_ok,
    make_cloid,
    make_exchange,
    make_info,
    parse_order_response,
    response_detail,
)
from tests.hl_envelopes import (
    C1_INSUFFICIENT_MARGIN,
    C1_MIN_NOTIONAL,
    MARGIN_UPDATE_ACCEPTED,
    UNIFIED_ACCOUNT_TRANSFER,
    UNKNOWN_ACCOUNT,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "delta0"


def _order(*statuses: Any) -> dict[str, Any]:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": list(statuses)}}}


# --- Clients: a timeout on every socket (audit dev 2026-09-16, 4.1) ---------------


def test_the_read_client_carries_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_info(api_url: str, **kwargs: Any) -> object:
        _ = api_url
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("delta0.hl_client.Info", _fake_info)
    make_info("https://api.hyperliquid.xyz", websocket=False)
    assert seen == {"skip_ws": True, "timeout": HL_HTTP_TIMEOUT_S}


def test_the_signing_client_carries_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_exchange(wallet: object, api_url: str, **kwargs: Any) -> object:
        _ = wallet, api_url
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("hyperliquid.exchange.Exchange", _fake_exchange)
    # A throwaway key: nothing is signed, the client is never used.
    make_exchange("0x" + "11" * 32, "https://api.hyperliquid.xyz")
    assert seen == {"timeout": HL_HTTP_TIMEOUT_S}


def test_a_client_order_id_is_derived_from_its_intent() -> None:
    first = make_cloid("intent-a").to_raw()
    assert re.fullmatch(r"0x[0-9a-f]{32}", first)
    assert make_cloid("intent-a").to_raw() == first
    assert make_cloid("intent-b").to_raw() != first


# --- First level ----------------------------------------------------------------


def test_an_accepted_action_passes_through() -> None:
    assert ensure_ok(MARGIN_UPDATE_ACCEPTED, "marge isolée") is MARGIN_UPDATE_ACCEPTED


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [(UNIFIED_ACCOUNT_TRANSFER, "unified account"), (UNKNOWN_ACCOUNT, "does not exist")],
)
def test_a_first_level_refusal_raises_with_the_venue_reason(
    envelope: dict[str, Any],
    reason: str,
) -> None:
    assert not is_ok(envelope)
    with pytest.raises(HLActionRefused, match=reason):
        ensure_ok(envelope, "action")


def test_the_action_name_reaches_the_message() -> None:
    """The caller's label is what an operator reads in the alert first."""
    with pytest.raises(HLActionRefused, match="retrait du pont"):
        ensure_ok(UNIFIED_ACCOUNT_TRANSFER, "retrait du pont")


@pytest.mark.parametrize(
    "result",
    [None, "ok", 42, [], {}, {"status": "error"}, {"response": "..."}],
)
def test_anything_that_is_not_an_explicit_ok_is_a_refusal(result: object) -> None:
    """An unrecognised shape means "we do not know that this worked"."""
    assert not is_ok(result)
    with pytest.raises(HLActionRefused):
        ensure_ok(result, "action")


def test_detail_survives_a_non_dict_response() -> None:
    assert response_detail("boom") == "boom"
    assert response_detail(UNIFIED_ACCOUNT_TRANSFER) == (
        "Action disabled when unified account is active"
    )


# --- Nested in a success (C1) ---------------------------------------------------


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [
        (C1_INSUFFICIENT_MARGIN, "Insufficient margin"),
        (C1_MIN_NOTIONAL, r"minimum value of \$10"),
    ],
)
def test_a_refusal_nested_in_an_ok_envelope_is_a_refusal(
    envelope: dict[str, Any],
    reason: str,
) -> None:
    """The two testnet refusals the first-level check let through."""
    assert not is_ok(envelope)
    with pytest.raises(HLActionRefused, match=reason):
        ensure_ok(envelope, "ordre IOC")


def test_one_refused_entry_refuses_the_whole_answer() -> None:
    mixed = _order({"resting": {"oid": 7}}, {"error": "Insufficient margin to place order."})
    with pytest.raises(HLActionRefused, match="Insufficient margin"):
        ensure_ok(mixed, "ordres")


@pytest.mark.parametrize("statuses", [[], "success", None])
def test_statuses_that_say_nothing_are_a_refusal(statuses: object) -> None:
    envelope = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
    with pytest.raises(HLActionRefused, match="sans statut exploitable"):
        ensure_ok(envelope, "ordre")


def test_a_cancel_acknowledged_by_plain_strings_passes() -> None:
    """Cancels acknowledge with `"success"` strings, not mappings."""
    cancelled = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
    assert is_ok(cancelled)


# --- Reading an order -------------------------------------------------------------


def test_a_resting_order_yields_its_id() -> None:
    outcome = parse_order_response(_order({"resting": {"oid": 12345}}), "ordre")
    assert outcome.order_id == 12345
    assert outcome.filled_size == 0.0


def test_a_filled_order_yields_its_id_and_size() -> None:
    outcome = parse_order_response(_order({"filled": {"oid": 42, "totalSz": "0.001"}}), "ordre")
    assert outcome.order_id == 42
    assert outcome.filled_size == pytest.approx(0.001)


def test_a_nested_refusal_never_becomes_an_order() -> None:
    with pytest.raises(HLActionRefused, match="Insufficient margin"):
        parse_order_response(C1_INSUFFICIENT_MARGIN, "ordre IOC")


@pytest.mark.parametrize(
    "envelope",
    [
        _order({"waitingForTrigger": {}}),
        {"status": "ok", "response": {"type": "order", "data": {}}},
        _order({"resting": {"oid": "not-a-number"}}),
    ],
)
def test_an_order_we_cannot_identify_is_a_refusal(envelope: dict[str, Any]) -> None:
    """Neither cancellable nor accountable: never a confirmed order."""
    with pytest.raises(HLActionRefused):
        parse_order_response(envelope, "ordre")


# --- The door stays single --------------------------------------------------------


def test_only_hl_client_imports_the_sdk() -> None:
    """Belt and braces with ruff's TID251, which a config edit could switch off."""
    importing = re.compile(r"^\s*(from|import)\s+hyperliquid\b", re.MULTILINE)
    offenders = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "hl_client.py" and importing.search(path.read_text(encoding="utf-8"))
    )
    assert not offenders, f"le SDK Hyperliquid est importé hors de hl_client.py : {offenders}"
