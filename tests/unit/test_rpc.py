"""RPC failover — hangs, benching, and the send that must not be replayed.

Written from a live incident (2026-09-03): the primary endpoint answered in
108 seconds instead of 53 ms, in bursts, 15 hours into the M1 run. It never
refused a request, so nothing keyed on exceptions would have reacted.
"""

from __future__ import annotations

import asyncio

import pytest
from web3.types import RPCEndpoint

from delta0.rpc import FailoverProvider

_OK = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
_BLOCK_NUMBER = RPCEndpoint("eth_blockNumber")
_SEND_RAW = RPCEndpoint("eth_sendRawTransaction")


class _FakeChild:
    """Stands in for an AsyncHTTPProvider with scriptable behaviour."""

    def __init__(self, behaviour: str = "ok", *, hang_s: float = 10.0) -> None:
        self.behaviour = behaviour
        self.hang_s = hang_s
        self.calls: list[str] = []

    async def make_request(self, method: RPCEndpoint, params: object) -> dict[str, object]:
        _ = params
        self.calls.append(str(method))
        if self.behaviour == "hang":
            await asyncio.sleep(self.hang_s)
        elif self.behaviour == "raise":
            raise ConnectionError("connexion refusée")
        return dict(_OK)

    async def is_connected(self, show_traceback: bool = False) -> bool:
        _ = show_traceback
        return self.behaviour == "ok"


def _provider(
    *children: _FakeChild,
    timeout_s: float = 0.05,
    cooldown_s: float = 60.0,
) -> FailoverProvider:
    urls = [f"https://rpc-{i}.example/v2/secret" for i in range(len(children))]
    p = FailoverProvider(urls, request_timeout_s=timeout_s, cooldown_s=cooldown_s)
    for endpoint, child in zip(p._endpoints, children, strict=True):
        endpoint.provider = child  # type: ignore[assignment]
    return p


@pytest.mark.asyncio
async def test_healthy_primary_is_used_alone() -> None:
    primary, backup = _FakeChild(), _FakeChild()
    p = _provider(primary, backup)

    assert await p.make_request(_BLOCK_NUMBER, []) == _OK

    assert primary.calls == ["eth_blockNumber"]
    assert backup.calls == []


@pytest.mark.asyncio
async def test_a_hang_fails_over_rather_than_waiting() -> None:
    """The incident's exact shape: no error, just no answer."""
    primary, backup = _FakeChild("hang", hang_s=30.0), _FakeChild()
    p = _provider(primary, backup, timeout_s=0.05)

    assert await p.make_request(_BLOCK_NUMBER, []) == _OK
    assert backup.calls == ["eth_blockNumber"]


@pytest.mark.asyncio
async def test_a_transport_error_fails_over_too() -> None:
    primary, backup = _FakeChild("raise"), _FakeChild()
    p = _provider(primary, backup)

    assert await p.make_request(_BLOCK_NUMBER, []) == _OK
    assert backup.calls == ["eth_blockNumber"]


@pytest.mark.asyncio
async def test_a_benched_endpoint_is_skipped_on_later_calls() -> None:
    """One failure must not cost a timeout on every subsequent request."""
    primary, backup = _FakeChild("hang", hang_s=30.0), _FakeChild()
    p = _provider(primary, backup, timeout_s=0.05, cooldown_s=60.0)

    await p.make_request(_BLOCK_NUMBER, [])
    await p.make_request(_BLOCK_NUMBER, [])
    await p.make_request(_BLOCK_NUMBER, [])

    # Tried once, benched, never tried again inside the cooldown.
    assert len(primary.calls) == 1
    assert len(backup.calls) == 3


@pytest.mark.asyncio
async def test_the_preferred_endpoint_is_reclaimed_after_the_cooldown() -> None:
    """Order is a preference, not a rotation — a run returns to the good one."""
    primary, backup = _FakeChild("hang", hang_s=30.0), _FakeChild()
    p = _provider(primary, backup, timeout_s=0.05, cooldown_s=60.0)

    await p.make_request(_BLOCK_NUMBER, [])
    assert len(backup.calls) == 1

    primary.behaviour = "ok"
    p._endpoints[0].unhealthy_until = 0.0  # cooldown élapsed

    await p.make_request(_BLOCK_NUMBER, [])
    assert len(primary.calls) == 2
    assert len(backup.calls) == 1


@pytest.mark.asyncio
async def test_a_send_is_never_replayed_on_another_endpoint() -> None:
    """A send that timed out may already be on-chain.

    Replaying it elsewhere returns `already known`, which web3 raises — the
    executor would mark failed a transaction that actually succeeded. Failing
    on the spot leaves the intent journal to reconcile it (README §13).
    """
    primary, backup = _FakeChild("hang", hang_s=30.0), _FakeChild()
    p = _provider(primary, backup, timeout_s=0.05)

    with pytest.raises(TimeoutError):
        await p.make_request(_SEND_RAW, ["0xdeadbeef"])

    assert primary.calls == ["eth_sendRawTransaction"]
    assert backup.calls == []


@pytest.mark.asyncio
async def test_all_endpoints_down_raises_the_last_error() -> None:
    primary, backup = _FakeChild("raise"), _FakeChild("raise")
    p = _provider(primary, backup)

    with pytest.raises(ConnectionError):
        await p.make_request(_BLOCK_NUMBER, [])


@pytest.mark.asyncio
async def test_a_single_endpoint_still_gets_its_timeout() -> None:
    """The deadline is the point; failover is only what follows it."""
    only = _FakeChild("hang", hang_s=30.0)
    p = _provider(only, timeout_s=0.05)

    with pytest.raises(TimeoutError):
        await p.make_request(_BLOCK_NUMBER, [])


def test_duplicate_urls_collapse() -> None:
    """A `.env` with the same value twice must not double the retry budget."""
    url = "https://rpc.example/v2/secret"
    p = FailoverProvider([url, url])
    assert len(p._endpoints) == 1


def test_an_empty_fallback_is_ignored() -> None:
    p = FailoverProvider(["https://rpc.example", ""])
    assert len(p._endpoints) == 1


def test_no_url_at_all_is_refused() -> None:
    with pytest.raises(ValueError, match="aucune URL RPC"):
        FailoverProvider(["", ""])


def test_the_api_key_never_reaches_a_log_line() -> None:
    """Endpoint labels go into warnings; the key must not travel with them."""
    p = FailoverProvider(["https://arb-mainnet.g.alchemy.com/v2/SECRET_KEY"])
    label = p._endpoints[0].label
    assert "SECRET_KEY" not in label
    assert label == "https://arb-mainnet.g.alchemy.com"
