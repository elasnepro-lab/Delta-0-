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


class _FundingPages:
    """`fundingHistory` as Hyperliquid serves it: 500 rows at most, from `startTime`."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    def funding_history(self, coin: str, start: int, end: int | None = None) -> Any:
        _ = coin
        self.calls += 1
        hits = [r for r in self.rows if r["time"] >= start and (end is None or r["time"] <= end)]
        return hits[:500]


_NOW_MS = 1_760_000_000_000 // 3_600_000 * 3_600_000 + 60_000


def _hourly(rate_of: Any) -> list[dict[str, Any]]:
    """720 hourly rows ending a minute before `_NOW_MS`, rate set per hour index."""
    first = _NOW_MS - 720 * 3_600_000
    return [{"time": first + i * 3_600_000, "fundingRate": str(rate_of(i))} for i in range(720)]


def _funding_reader(pages: _FundingPages, monkeypatch: pytest.MonkeyPatch) -> HyperliquidReader:
    reader = HyperliquidReader.__new__(HyperliquidReader)
    reader._user = "0x000000000000000000000000000000000000dEaD"
    reader._info = pages
    monkeypatch.setattr(HyperliquidReader, "_now_ms", staticmethod(lambda: _NOW_MS))
    return reader


@pytest.mark.asyncio
async def test_the_30_day_funding_reads_the_whole_window_not_its_oldest_500_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revue finance 2026-10-02, F2: one call stopped at the 500 OLDEST rows.

    The oldest 500 hours pay 1e-5, the last 220 pay 3e-5. Unpaged, the mean
    was 1e-5 and missed the recent rise entirely.
    """
    pages = _FundingPages(_hourly(lambda i: 1e-5 if i < 500 else 3e-5))
    reader = _funding_reader(pages, monkeypatch)
    expected = (500 * 1e-5 + 220 * 3e-5) / 720 * 8760
    assert await reader.read_funding_avg_30d("ETH") == pytest.approx(expected)
    assert pages.calls == 2


@pytest.mark.asyncio
async def test_the_30_day_funding_is_read_once_per_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It cannot move inside an hour; paging it every 5 s cycle spends the rate limit."""
    pages = _FundingPages(_hourly(lambda i: 1e-5))
    reader = _funding_reader(pages, monkeypatch)
    first = await reader.read_funding_avg_30d("ETH")
    assert await reader.read_funding_avg_30d("ETH") == first
    assert pages.calls == 2

    monkeypatch.setattr(HyperliquidReader, "_now_ms", staticmethod(lambda: _NOW_MS + 3_600_000))
    await reader.read_funding_avg_30d("ETH")
    assert pages.calls == 4


@pytest.mark.asyncio
async def test_a_funding_page_that_goes_back_in_time_is_a_venue_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Backwards(_FundingPages):
        def funding_history(self, coin: str, start: int, end: int | None = None) -> Any:
            _ = coin, end
            return [{"time": start - 1, "fundingRate": "0.00001"}] * 500

    reader = _funding_reader(_Backwards([]), monkeypatch)
    with pytest.raises(HLReadError, match="en arrière"):
        await reader.read_funding_avg_30d("ETH")


@pytest.mark.asyncio
async def test_the_free_reserve_excludes_the_margin_already_committed() -> None:
    """On a unified account `total` includes the isolated margin, held as `hold`.

    Revue finance 2026-10-02, F1. Reading `total` alone counted the margin
    twice in the equity and gave P2 a reserve that was not there.
    """
    reader = _reader(
        spot_user_state={
            "balances": [
                {"coin": "HYPE", "total": "3.0", "hold": "0.0"},
                {"coin": "USDC", "total": "5429.0", "hold": "4176.0"},
            ]
        }
    )
    assert await reader.read_free_usdc() == pytest.approx(1253.0)


@pytest.mark.asyncio
async def test_the_free_reserve_never_goes_negative() -> None:
    reader = _reader(
        spot_user_state={"balances": [{"coin": "USDC", "total": "10.0", "hold": "10.5"}]}
    )
    assert await reader.read_free_usdc() == 0.0


def _position_state(szi: str) -> dict[str, Any]:
    return {
        "assetPositions": [
            {
                "position": {
                    "coin": "ETH",
                    "szi": szi,
                    "entryPx": "2500.0",
                    "marginUsed": "5000.0",
                    "leverage": {"type": "isolated", "value": 10},
                }
            }
        ]
    }


@pytest.mark.asyncio
async def test_a_short_reads_as_a_positive_short_size() -> None:
    position = await _reader(user_state=_position_state("-20.0")).read_position("ETH")
    assert position is not None
    assert position.short_size_eth == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_a_long_keeps_its_sign_instead_of_passing_for_a_short() -> None:
    """Revue finance 2026-10-02, F3: `abs()` read this long as a 20 ETH short."""
    position = await _reader(user_state=_position_state("20.0")).read_position("ETH")
    assert position is not None
    assert position.short_size_eth == pytest.approx(-20.0)
