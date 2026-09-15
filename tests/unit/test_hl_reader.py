"""A malformed Hyperliquid read is an outage the loop survives, not a crash.

Found in the 2026-09-14 review: the SDK RETURNS `{"error": "Could not parse
JSON: ..."}` instead of raising, and the readers then died on a missing key.
Since chantier 6.4 a KeyError stops the process, so a proxy glitch would have
taken the bot down.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from delta0.failure import OPERATIONAL_ERRORS
from delta0.hl_client import HLReadError
from delta0.venues.hyperliquid import HyperliquidReader


class _FakeInfo:
    def __init__(self, **answers: Any) -> None:
        self._answers = answers

    def __getattr__(self, name: str) -> Any:
        answer = self._answers[name]

        def call(*args: Any) -> Any:
            _ = args
            return answer

        return call


def _reader(**answers: Any) -> HyperliquidReader:
    reader = HyperliquidReader.__new__(HyperliquidReader)
    reader._user = "0x000000000000000000000000000000000000dEaD"
    reader._info = _FakeInfo(**answers)
    return reader


class _HungInfo:
    def all_mids(self) -> dict[str, str]:
        time.sleep(0.3)
        return {"ETH": "2500"}


@pytest.mark.asyncio
async def test_a_hung_call_is_a_timeout_the_loop_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket that never answers froze the snapshot, and the KILL file with it.

    Audit dev 2026-09-16, 4.1: the SDK has no HTTP timeout by default, and the
    thread offload was never bounded.
    """
    monkeypatch.setattr("delta0.venues.hyperliquid._CALL_DEADLINE_S", 0.05)
    reader = HyperliquidReader.__new__(HyperliquidReader)
    reader._user = "0x000000000000000000000000000000000000dEaD"
    reader._info = _HungInfo()

    with pytest.raises(TimeoutError) as excinfo:
        await reader.read_mark_price("ETH")
    assert issubclass(excinfo.type, OPERATIONAL_ERRORS)


@pytest.mark.asyncio
async def test_the_sdk_parse_error_envelope_becomes_a_venue_error() -> None:
    reader = _reader(all_mids={"error": "Could not parse JSON: <html>"})
    with pytest.raises(HLReadError, match="Could not parse JSON") as excinfo:
        await reader.read_mark_price("ETH")
    assert issubclass(excinfo.type, OPERATIONAL_ERRORS)


@pytest.mark.asyncio
async def test_a_missing_coin_is_a_venue_error_not_a_key_error() -> None:
    with pytest.raises(HLReadError, match="ETH"):
        await _reader(all_mids={"BTC": "60000"}).read_mark_price("ETH")


@pytest.mark.asyncio
async def test_a_malformed_funding_history_is_a_venue_error() -> None:
    reader = _reader(funding_history=[{"unexpected": "shape"}])
    with pytest.raises(HLReadError, match="funding"):
        await reader.read_funding_avg_30d("ETH")
    with pytest.raises(HLReadError, match="funding"):
        await reader.read_last_hour_funding("ETH")


@pytest.mark.asyncio
async def test_a_well_formed_read_still_reads() -> None:
    reader = _reader(
        all_mids={"ETH": "2509.35"},
        funding_history=[{"fundingRate": "0.0000125"}],
    )
    assert await reader.read_mark_price("ETH") == pytest.approx(2509.35)
    assert await reader.read_last_hour_funding("ETH") == pytest.approx(0.0000125)
