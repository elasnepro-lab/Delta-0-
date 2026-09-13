"""The one module allowed to import the Hyperliquid SDK.

Two things make the SDK dangerous to call directly, and both were paid for.

**It reports refusals by return value.** `{"status": "err", "response": "..."}`
comes back for every action — an order, a bridge withdrawal, a transfer —
where code written around `try/except` sees a success. A spot-to-perp transfer
the venue declined was logged as done, and a rejected post-only order would
have left a P1/P2 latency sample in the M1 report for an order that never
existed.

**And sometimes the refusal hides inside a success.** On an account that
exists, an order refused for insufficient margin or a notional under 10 $
comes back as

    {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Insufficient margin to place order. asset=4"}]}}}

Confirmed on the testnet on 2026-09-08 (memory/hl_findings.md §7, audit C1).
A first-level check lets it through: an emergency IOC refused for margin would
have been journaled as confirmed.

Every SDK call goes through here so that both checks are unavoidable; ruff's
TID251 refuses `import hyperliquid` anywhere else in `src/`. The message is
matched by the presence of an `error` key, never by its text: the venue writes
it in unstructured English.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hyperliquid.info import Info

if TYPE_CHECKING:
    from hyperliquid.exchange import Exchange

# Re-exported for type annotations, so callers never import the SDK themselves.
HLInfo = Info


class HLActionRefused(Exception):  # noqa: N818 - mirrors SafetyRefused naming
    """Hyperliquid accepted the request and declined to perform it."""


# --- Clients ------------------------------------------------------------------


def make_info(api_url: str, *, websocket: bool) -> Info:
    """Read client. `websocket=True` starts the SDK's WebSocket thread."""
    return Info(api_url, skip_ws=not websocket)


def make_exchange(private_key: str, api_url: str) -> Exchange:
    """Signing client. Build it once per executor, never per order.

    Its constructor fetches perp and spot metadata over HTTP: rebuilt per order
    it put two round trips inside the P1/P2 window (memory/hl_findings.md §3).
    The imports are local so that a rehearsal, which never signs, never loads
    the signing libraries either.
    """
    from eth_account import Account  # noqa: PLC0415
    from hyperliquid.exchange import Exchange as _Exchange  # noqa: PLC0415

    return _Exchange(Account.from_key(private_key), api_url)


# --- Reading responses ----------------------------------------------------------


def response_detail(result: Any) -> str:
    """Human-readable reason out of an SDK response, whatever its shape."""
    if isinstance(result, dict):
        return str(result.get("response", result))
    return str(result)


def _refusal(result: Any) -> str | None:
    """Why the venue declined, or None when it accepted at every level."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        # Not a mapping means the SDK's contract changed under us; the safe
        # reading of an unknown answer, when funds may have moved, is failure.
        return response_detail(result)

    response = result.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or "statuses" not in data:
        # A withdrawal or a margin update answers {"type": "default"}: nothing nested.
        return None

    statuses = data["statuses"]
    if not isinstance(statuses, list) or not statuses:
        return f"réponse sans statut exploitable : {statuses!r}"
    errors = [
        str(entry["error"]) for entry in statuses if isinstance(entry, dict) and "error" in entry
    ]
    if errors:
        # One refused entry refuses the whole answer. The bot sends one order
        # per request; a batch half-accepted must not read as a success.
        return "; ".join(errors)
    return None


def is_ok(result: Any) -> bool:
    """True when the venue accepted the action, nested statuses included."""
    return _refusal(result) is None


def ensure_ok(result: Any, action: str) -> dict[str, Any]:
    """Return the response, or raise `HLActionRefused` naming the venue's reason."""
    refusal = _refusal(result)
    if refusal is None:
        assert isinstance(result, dict)
        return result
    raise HLActionRefused(f"{action} refusé par Hyperliquid : {refusal}")


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    """What an accepted single order became."""

    order_id: int
    filled_size: float  # 0.0 while the order rests on the book


def parse_order_response(result: Any, action: str) -> OrderOutcome:
    """Order id and filled size of an accepted order, or `HLActionRefused`.

    An accepted answer that names neither a resting nor a filled order is a
    refusal as well: an order we cannot identify can be neither cancelled nor
    accounted for.
    """
    body = ensure_ok(result, action)
    response = body.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list):
        raise HLActionRefused(f"{action} : réponse sans statut d'ordre ({response_detail(body)})")

    order_id: int | None = None
    filled = 0.0
    try:
        for entry in statuses:
            if not isinstance(entry, dict):
                continue
            if "resting" in entry and order_id is None:
                order_id = int(entry["resting"]["oid"])
            if "filled" in entry:
                if order_id is None:
                    order_id = int(entry["filled"]["oid"])
                filled += float(entry["filled"].get("totalSz", 0.0))
    except (KeyError, TypeError, ValueError) as e:
        raise HLActionRefused(f"{action} : statut d'ordre illisible ({statuses!r})") from e

    if order_id is None:
        raise HLActionRefused(f"{action} : ni ordre posé ni exécution ({statuses!r})")
    return OrderOutcome(order_id=order_id, filled_size=filled)
