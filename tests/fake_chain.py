"""Un nœud JSON-RPC en mémoire, partagé par les tests des lectures on-chain.

Même rôle que `tests/binance_archives.py` pour les archives : un seul endroit
décide comment la chaîne répond, pour que deux tests ne mesurent pas deux
chaînes différentes. Ce nœud sait dater ses blocs, changer de rythme en cours de
route (la fusion d'Ethereum, la migration Nitro), refuser une plage trop dense,
tomber en panne et compter ses appels.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from backtest.chain import Chain

GENESIS_TS = 1_600_000_000
ALIVE = "https://premier.invalid"
SPARE = "https://second.invalid"

POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
TOPIC = "0x804c9b842b2748a22bb64b345453a3de7ca54a6ca45ce00d415894979e22897a"
RAY = 10**27


def reserve_data(block: int) -> str:
    """Le champ `data` d'un ReserveDataUpdated : cinq mots, index croissants avec le bloc."""
    mots = (
        2 * RAY // 100,  # liquidityRate
        0,  # stableBorrowRate
        4 * RAY // 100,  # variableBorrowRate
        RAY + block * 10**18,  # liquidityIndex
        RAY + block * 2 * 10**18,  # variableBorrowIndex
    )
    return "0x" + "".join(f"{mot:064x}" for mot in mots)


class FakeNode:
    """Un nœud JSON-RPC en mémoire : horodatages réguliers, logs tous les N blocs."""

    def __init__(
        self,
        *,
        head: int = 1_000_000,
        genesis_ts: int = GENESIS_TS,
        seconds_per_block: int = 12,
        pivot: int | None = None,
        seconds_before_pivot: int = 13,
        max_span: int | None = None,
        log_every: int = 1_000,
        down: tuple[str, ...] = (),
        transient: int = 0,
        refuse_all: bool = False,
        call_value: Callable[[int], int] | None = None,
    ) -> None:
        self.head = head
        self.genesis_ts = genesis_ts
        self.seconds_per_block = seconds_per_block
        self.pivot = pivot
        self.seconds_before_pivot = seconds_before_pivot
        self.max_span = max_span
        self.log_every = log_every
        self.down = down
        self.transient = transient
        self.refuse_all = refuse_all
        self.call_value = call_value
        self.methods: list[str] = []
        self.urls: list[str] = []
        self.queries: list[dict[str, Any]] = []

    def timestamp(self, block: int) -> int:
        """Horodatage du bloc, avec au besoin un changement de rythme en cours de route."""
        if self.pivot is None:
            return self.genesis_ts + block * self.seconds_per_block
        before = min(block, self.pivot) * self.seconds_before_pivot
        after = max(0, block - self.pivot) * self.seconds_per_block
        return self.genesis_ts + before + after

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if any(dead in url for dead in self.down):
            return httpx.Response(503)
        if self.transient > 0:
            self.transient -= 1
            return httpx.Response(429)
        body = json.loads(request.content)
        self.methods.append(body["method"])
        return self.respond(body["method"], body["params"])

    def respond(self, method: str, params: list[Any]) -> httpx.Response:
        if self.refuse_all:
            return self.error("pruned history")
        if method == "eth_blockNumber":
            return self.ok(hex(self.head))
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return self.ok({"number": params[0], "timestamp": hex(self.timestamp(block))})
        if method == "eth_call":
            block = int(params[1], 16)
            value = self.call_value(block) if self.call_value is not None else 10**18
            return self.ok("0x" + f"{value:064x}")
        if method == "eth_getLogs":
            return self.logs(params[0])
        return self.error(f"méthode inconnue : {method}")

    def logs(self, query: dict[str, Any]) -> httpx.Response:
        self.queries.append(query)
        start, stop = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        if self.max_span is not None and stop - start + 1 > self.max_span:
            return self.error("log query timed out")
        first = start + (-start % self.log_every)
        return self.ok(
            [
                {"blockNumber": hex(block), "topics": [TOPIC], "data": reserve_data(block)}
                for block in range(first, stop + 1, self.log_every)
            ]
        )

    @staticmethod
    def ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    @staticmethod
    def error(message: str) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": message}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def chain_on(node: FakeNode, root: Path, *, endpoints: tuple[str, ...] = (ALIVE,)) -> Chain:
    return Chain("essai", node.client(), endpoints=endpoints, root=root, pause=lambda _: None)


def reference_block(node: FakeNode, target_ts: int) -> int:
    """Le bon bloc, trouvé bêtement, pour juger celui trouvé intelligemment."""
    low, high = 0, node.head
    while high - low > 1:
        middle = (low + high) // 2
        if node.timestamp(middle) <= target_ts:
            low = middle
        else:
            high = middle
    return low
