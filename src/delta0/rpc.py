"""RPC failover across several Arbitrum endpoints.

`.env` has declared `ARBITRUM_RPC_FALLBACK` since M0, and no line of code ever
read it. A single provider outage therefore degraded or killed a run, with the
watchdog's `rpc_fail_s` knob (README §11) having nothing to switch to.

Observed on 2026-09-03, 15 hours into the M1 marche à blanc: the primary
endpoint started answering in 108 SECONDS instead of 53 ms, in bursts. The
tracer survived, but individual snapshots took 165 s, one Aave approve took
219 s, and a 13-minute hole opened in the measurements. A second endpoint,
tested at the same moment, answered in 121-151 ms throughout.

Two lessons are built into this module:

**A hang is not an error.** The endpoint never refused a request — it accepted
and sat on it. Retry logic keyed on exceptions is blind to that, which is why
every request here carries a hard `asyncio.wait_for` deadline. The timeout is
the mechanism; failover is only what happens once it fires.

**Sends must not be retried elsewhere.** Re-broadcasting the same signed
transaction is idempotent — same nonce, same hash — but a send that timed out
client-side after landing on-chain would come back from the second endpoint as
`already known`, which web3 raises. The executor would mark the intent failed
for a transaction that actually succeeded, and the two would only be reconciled
at next boot (README §13). Cleaner to let a send fail on the spot: the intent
journal already handles exactly that case.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from web3.providers.async_base import AsyncBaseProvider
from web3.providers.rpc import AsyncHTTPProvider
from web3.types import RPCEndpoint, RPCResponse

from delta0.logging import get_logger

log = get_logger(__name__)


# Normal Arbitrum reads answer in 50-150 ms. Ten seconds is far outside that
# band while staying above a slow-but-working call, so it fires on hangs and
# not on a bad minute.
DEFAULT_REQUEST_TIMEOUT_S = 10.0

# How long a failing endpoint stays benched. Long enough that a bad episode
# does not cause constant flapping between providers, short enough that the
# preferred endpoint is reclaimed within a few minutes of recovering.
DEFAULT_COOLDOWN_S = 180.0

# Methods that must never be replayed on another endpoint — see module docstring.
NON_FAILOVER_METHODS: frozenset[str] = frozenset({"eth_sendRawTransaction"})


@dataclass
class _Endpoint:
    url: str
    provider: AsyncHTTPProvider
    unhealthy_until: float = field(default=0.0)

    def healthy(self, now: float) -> bool:
        return now >= self.unhealthy_until

    @property
    def label(self) -> str:
        """Endpoint identity safe to log — the API key never appears."""
        head, _, _tail = self.url.partition("/v2/")
        return head if head != self.url else self.url


class FailoverProvider(AsyncBaseProvider):
    """Sends every request to the first healthy endpoint, in declared order.

    Order is a preference, not a rotation: index 0 is used whenever it is
    healthy, so a run returns to the good endpoint on its own once a bad
    episode passes.
    """

    def __init__(
        self,
        urls: list[str],
        *,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        super().__init__()
        kept = [u for u in urls if u]
        if not kept:
            raise ValueError("aucune URL RPC fournie")
        self._timeout_s = request_timeout_s
        self._cooldown_s = cooldown_s
        self._endpoints = [
            _Endpoint(url=u, provider=AsyncHTTPProvider(u)) for u in dict.fromkeys(kept)
        ]

    @property
    def endpoint_uri(self) -> str:
        """The endpoint currently in use. Web3 and our logs both read this."""
        return self._endpoints[self._current_index()].url

    def _current_index(self) -> int:
        now = time.monotonic()
        for i, ep in enumerate(self._endpoints):
            if ep.healthy(now):
                return i
        # Every endpoint is benched: fall back to the preferred one rather than
        # refusing outright. A stale bench is not a reason to stop trying.
        return 0

    def _bench(self, ep: _Endpoint, reason: str) -> None:
        ep.unhealthy_until = time.monotonic() + self._cooldown_s
        log.warning(
            "rpc_endpoint_benched",
            message=(f"RPC {ep.label} écarté pour {self._cooldown_s:.0f} s — {reason}"),
            endpoint=ep.label,
            reason=reason,
            cooldown_s=self._cooldown_s,
        )

    async def _attempt(self, ep: _Endpoint, method: RPCEndpoint, params: Any) -> RPCResponse:
        # The child provider's own middleware is bypassed on purpose: this
        # provider owns the stack web3 sees, and a nested one would cache and
        # retry a second time behind our back.
        return await asyncio.wait_for(
            ep.provider.make_request(method, params),
            timeout=self._timeout_s,
        )

    async def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        start = self._current_index()
        order = [self._endpoints[start]]
        if str(method) not in NON_FAILOVER_METHODS:
            order += [ep for i, ep in enumerate(self._endpoints) if i != start]

        last_error: BaseException | None = None
        for ep in order:
            try:
                response = await self._attempt(ep, method, params)
            except TimeoutError as e:
                last_error = e
                self._bench(ep, f"pas de réponse en {self._timeout_s:.0f} s sur {method}")
            except Exception as e:
                last_error = e
                self._bench(ep, f"{type(e).__name__} sur {method}")
            else:
                if ep is not self._endpoints[start]:
                    log.info(
                        "rpc_failover",
                        message=f"requête {method} servie par le RPC de secours {ep.label}",
                        endpoint=ep.label,
                        method=str(method),
                    )
                return response

        assert last_error is not None
        raise last_error

    async def is_connected(self, show_traceback: bool = False) -> bool:
        for ep in self._endpoints:
            if await ep.provider.is_connected(show_traceback):
                return True
        return False


def build_provider(
    primary: str,
    fallback: str = "",
    *,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
) -> FailoverProvider:
    """Provider for the configured endpoints, primary first.

    A single endpoint is still wrapped: the per-request timeout matters on its
    own, and it is what turns a 108-second hang into a prompt failure the
    watchdog can act on.
    """
    return FailoverProvider([primary, fallback], request_timeout_s=request_timeout_s)
