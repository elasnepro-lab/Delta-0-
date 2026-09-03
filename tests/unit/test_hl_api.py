"""The Hyperliquid SDK reports refusals by return value, not by exception.

Every helper here exists because of one live incident: a spot-to-perp transfer
that Hyperliquid declined — `{'status': 'err', 'response': 'Action disabled
when unified account is active'}` — and that the bot logged as done, because
the surrounding code only watched for exceptions.
"""

from __future__ import annotations

import pytest

from delta0.hl_api import HLActionRefused, ensure_ok, is_ok, response_detail

_REFUSAL = {
    "status": "err",
    "response": "Action disabled when unified account is active",
}
_ACCEPTED = {"status": "ok", "response": {"type": "order", "data": {}}}


def test_accepted_response_passes_through() -> None:
    assert ensure_ok(_ACCEPTED, "ordre") is _ACCEPTED


def test_refusal_raises_with_the_venue_reason() -> None:
    with pytest.raises(HLActionRefused, match="unified account"):
        ensure_ok(_REFUSAL, "transfert")


def test_the_action_name_reaches_the_message() -> None:
    """The caller's label is what an operator reads in the log first."""
    with pytest.raises(HLActionRefused, match="retrait du pont"):
        ensure_ok(_REFUSAL, "retrait du pont")


@pytest.mark.parametrize(
    "result",
    [None, "ok", 42, [], {}, {"status": "error"}, {"response": "..."}],
)
def test_anything_that_is_not_an_explicit_ok_is_a_refusal(result: object) -> None:
    """An unrecognised shape means "we do not know that this worked".

    The SDK always returns a mapping for these actions, so a different shape
    means it changed under us — and the safe reading of an unknown answer,
    when real funds moved, is failure.
    """
    assert not is_ok(result)
    with pytest.raises(HLActionRefused):
        ensure_ok(result, "action")


def test_detail_survives_a_non_dict_response() -> None:
    assert response_detail("boom") == "boom"
    assert response_detail(_REFUSAL) == "Action disabled when unified account is active"
