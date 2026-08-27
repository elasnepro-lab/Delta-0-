"""HyperliquidStream — queue mechanics, callback parsing.

We do not spin up a real WS here: the callbacks are invoked directly to
mimic what the SDK would deliver, keeping the tests hermetic.
"""

from __future__ import annotations

import asyncio

import pytest

from delta0.venues.hl_stream import HyperliquidStream


@pytest.fixture
async def stream() -> HyperliquidStream:
    # Async fixture: guarantees a running event loop when we grab it.
    s = HyperliquidStream(
        api_url="https://api.hyperliquid.xyz",
        user_address="0x0000000000000000000000000000000000000001",
        coin="ETH",
    )
    s._loop = asyncio.get_running_loop()
    return s


@pytest.mark.asyncio
async def test_on_mids_parses_price(stream: HyperliquidStream) -> None:
    stream._on_mids({"channel": "allMids", "data": {"mids": {"ETH": "2500.5", "BTC": "68000"}}})
    tick = await stream.next_mark_tick(timeout_s=0.1)
    assert tick is not None
    assert tick.coin == "ETH"
    assert tick.mark_price == pytest.approx(2500.5)


@pytest.mark.asyncio
async def test_on_mids_ignores_unknown_coin(stream: HyperliquidStream) -> None:
    stream._on_mids({"channel": "allMids", "data": {"mids": {"BTC": "68000"}}})
    tick = await stream.next_mark_tick(timeout_s=0.05)
    assert tick is None


@pytest.mark.asyncio
async def test_on_mids_ignores_malformed(stream: HyperliquidStream) -> None:
    stream._on_mids({"channel": "allMids"})  # no data
    stream._on_mids({"data": "not-a-dict"})
    stream._on_mids({"data": {"mids": {"ETH": "not-a-number"}}})
    tick = await stream.next_mark_tick(timeout_s=0.05)
    assert tick is None


@pytest.mark.asyncio
async def test_try_latest_returns_freshest(stream: HyperliquidStream) -> None:
    for i in range(5):
        stream._on_mids({"data": {"mids": {"ETH": str(2500 + i)}}})
    latest = stream.try_latest_mark()
    assert latest is not None
    assert latest.mark_price == pytest.approx(2504)
    # Queue is drained now.
    assert stream.try_latest_mark() is None


@pytest.mark.asyncio
async def test_user_event_liquidation(stream: HyperliquidStream) -> None:
    stream._on_user_event({"channel": "userEvents", "data": {"liquidation": {"px": "2500"}}})
    events = stream.drain_user_events()
    assert len(events) == 1
    assert events[0].kind == "liquidation"


@pytest.mark.asyncio
async def test_user_event_fill_vs_funding_vs_unknown(stream: HyperliquidStream) -> None:
    stream._on_user_event({"data": {"fills": [{"sz": "1.0"}]}})
    stream._on_user_event({"data": {"funding": {"usdc": "0.5"}}})
    stream._on_user_event({"data": {"weird": {}}})
    events = stream.drain_user_events()
    kinds = [e.kind for e in events]
    assert kinds == ["fill", "funding", "unknown"]


@pytest.mark.asyncio
async def test_drop_oldest_on_overflow(stream: HyperliquidStream) -> None:
    # Fill beyond capacity — oldest should be evicted, latest preserved.
    for i in range(300):
        stream._on_mids({"data": {"mids": {"ETH": str(2000 + i)}}})
    latest = stream.try_latest_mark()
    assert latest is not None
    assert latest.mark_price == pytest.approx(2299)


@pytest.mark.asyncio
async def test_drain_user_events_empty(stream: HyperliquidStream) -> None:
    assert stream.drain_user_events() == []


@pytest.mark.asyncio
async def test_next_mark_tick_timeout(stream: HyperliquidStream) -> None:
    result = await stream.next_mark_tick(timeout_s=0.01)
    assert result is None


@pytest.mark.asyncio
async def test_next_user_event_timeout(stream: HyperliquidStream) -> None:
    result = await stream.next_user_event(timeout_s=0.01)
    assert result is None
