"""Reading Hyperliquid SDK responses.

The SDK does not raise on rejection. It RETURNS an error envelope:

    {"status": "err", "response": "Action disabled when unified account is active"}

and it does so for every action — placing an order, withdrawing from the
bridge, transferring between sub-accounts. Code written around `try/except`
therefore treats a refusal as a success. That is not a hypothetical: the first
live spot-to-perp transfer was refused by the venue and logged as done, and a
rejected post-only order would have been journaled as a confirmed intent
carrying a fabricated P1/P2 latency sample — a number in the M1 report standing
for an order that never existed.

Every call into the SDK goes through `ensure_ok` so a refusal becomes an
exception at the call site, where the surrounding code already knows how to
mark the intent failed.
"""

from __future__ import annotations

from typing import Any


class HLActionRefused(Exception):  # noqa: N818 - mirrors SafetyRefused naming
    """Hyperliquid accepted the request and declined to perform it."""


def response_detail(result: Any) -> str:
    """Human-readable reason out of an SDK response, whatever its shape."""
    if isinstance(result, dict):
        return str(result.get("response", result))
    return str(result)


def is_ok(result: Any) -> bool:
    """True when the venue reports the action as accepted.

    A non-dict response is treated as NOT ok: the SDK always returns a mapping
    for these actions, so anything else means the shape changed under us and
    the safe reading is "we do not know that this worked".
    """
    return isinstance(result, dict) and result.get("status") == "ok"


def ensure_ok(result: Any, action: str) -> dict[str, Any]:
    """Return the response, or raise `HLActionRefused` describing the refusal."""
    if is_ok(result):
        assert isinstance(result, dict)
        return result
    raise HLActionRefused(f"{action} refusé par Hyperliquid : {response_detail(result)}")
