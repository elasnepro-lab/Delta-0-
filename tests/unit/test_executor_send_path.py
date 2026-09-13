"""The live send path of AaveTraceExecutor, which no dry run ever exercised.

Every executor test ran with `dry_run=True`, so the branch that signs, sends and
waits for a receipt first met a chain on the first live shot of M1 — and died
there on `signed.rawTransaction`, web3 v6's name for what web3 v7 calls
`raw_transaction`. The fakes below expose web3 v7's shapes and nothing else,
so that kind of mistake fails here first (chantier 6.3).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from web3.exceptions import ContractLogicError

from delta0.config import Config
from delta0.executor import AaveTraceExecutor
from delta0.gas import with_gas_margin
from delta0.safety import MicroOpsGuard
from delta0.state import StateStore

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
MASTER = "0x000000000000000000000000000000000000dEaD"
PRIVATE_KEY = "0x" + "11" * 32
TX_HASH = bytes.fromhex("ab" * 32)
SIGNED_BYTES = b"\x02signed-by-the-fake"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@dataclass
class _SignedTransaction:
    """web3 v7's shape: `raw_transaction` — and no `rawTransaction`."""

    raw_transaction: bytes


class _Call:
    """A contract call: builds its transaction, and can be replayed for a diagnosis."""

    def __init__(self, *, gas: int = 100_000, replay_error: Exception | None = None) -> None:
        self.gas = gas
        self.replay_error = replay_error

    async def build_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        return {**params, "gas": self.gas}

    async def call(self, tx: dict[str, Any], block_identifier: Any = None) -> int:
        _ = tx, block_identifier
        if self.replay_error is not None:
            raise self.replay_error
        return 0


class _Decimals:
    async def call(self) -> int:
        return 6


class _Functions:
    def __init__(self, call: _Call) -> None:
        self._call = call

    def decimals(self) -> _Decimals:
        return _Decimals()

    def supply(self, *args: object) -> _Call:
        _ = args
        return self._call


class _Contract:
    def __init__(self, functions: _Functions) -> None:
        self.functions = functions


class _Account:
    def __init__(self) -> None:
        self.signed: list[dict[str, Any]] = []

    def sign_transaction(self, tx: dict[str, Any], private_key: str) -> _SignedTransaction:
        _ = private_key
        self.signed.append(tx)
        return _SignedTransaction(raw_transaction=SIGNED_BYTES)


class _Eth:
    def __init__(
        self,
        call: _Call,
        receipt: dict[str, Any],
        *,
        send_error: Exception | None = None,
    ) -> None:
        self.account = _Account()
        self._functions = _Functions(call)
        self._receipt = receipt
        self._send_error = send_error
        self.sent: list[bytes] = []

    def contract(self, address: str, abi: object) -> _Contract:
        _ = address, abi
        return _Contract(self._functions)

    async def get_transaction_count(self, address: str) -> int:
        _ = address
        return 7

    async def send_raw_transaction(self, raw: bytes) -> bytes:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(raw)
        return TX_HASH

    async def wait_for_transaction_receipt(self, tx_hash: bytes) -> dict[str, Any]:
        _ = tx_hash
        return self._receipt


def _live_executor(
    config: Config,
    store: StateStore,
    tmp_path: Path,
    eth: _Eth,
    *,
    private_key: str | None = PRIVATE_KEY,
) -> AaveTraceExecutor:
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={"dry_run": False, "require_first_use_confirmation": False},
            ),
        },
    )
    w3 = MagicMock()
    w3.eth = eth
    return AaveTraceExecutor(
        web3=w3,
        config=cfg,
        store=store,
        guard=MicroOpsGuard(config=cfg.tracer, project_root=tmp_path),
        master_address=MASTER,
        chain_id=42161,
        private_key=private_key,
    )


async def _only_intent(store: StateStore) -> tuple[str, str | None, str | None]:
    assert store._conn is not None
    cursor = await store._conn.execute("SELECT status, tx_hashes, failure FROM intents")
    rows = list(await cursor.fetchall())
    assert len(rows) == 1
    status, tx_hashes, cause = rows[0]
    return str(status), tx_hashes, cause


@pytest.mark.asyncio
async def test_a_confirmed_send_signs_sends_and_journals_the_hash(
    config: Config, store: StateStore, tmp_path: Path
) -> None:
    eth = _Eth(_Call(gas=100_000), {"status": 1, "gasUsed": 21_000})
    result = await _live_executor(config, store, tmp_path, eth).supply(USDC, 5.0)

    assert result.status == "confirmed"
    assert result.tx_hash == TX_HASH.hex()
    assert result.gas_used == 21_000
    assert eth.sent == [SIGNED_BYTES]  # the bytes web3 v7 names `raw_transaction`

    (signed,) = eth.account.signed
    assert signed["from"] == MASTER
    assert signed["nonce"] == 7
    assert signed["chainId"] == 42161
    assert signed["gas"] == with_gas_margin(100_000)

    status, tx_hashes, cause = await _only_intent(store)
    assert status == "confirmed"
    assert json.loads(tx_hashes or "[]") == [TX_HASH.hex()]
    assert cause is None
    assert (await store.latency_stats("path.aave_supply"))["count"] == 1


@pytest.mark.asyncio
async def test_a_reverted_transaction_fails_with_its_decoded_cause(
    config: Config, store: StateStore, tmp_path: Path
) -> None:
    revert = ContractLogicError("execution reverted: 0x6679996d", data="0x6679996d")
    eth = _Eth(_Call(replay_error=revert), {"status": 0, "gasUsed": 90_000, "blockNumber": 123})
    result = await _live_executor(config, store, tmp_path, eth).supply(USDC, 5.0)

    assert result.status == "failed"
    status, tx_hashes, cause = await _only_intent(store)
    assert status == "failed"
    assert json.loads(tx_hashes or "[]") == [TX_HASH.hex()]
    assert cause is not None
    assert "HealthFactorLowerThanLiquidationThreshold" in cause


@pytest.mark.asyncio
async def test_a_send_that_raises_is_journaled_failed_then_re_raised(
    config: Config, store: StateStore, tmp_path: Path
) -> None:
    eth = _Eth(_Call(), {"status": 1}, send_error=ConnectionResetError("RPC coupé"))
    with pytest.raises(ConnectionResetError):
        await _live_executor(config, store, tmp_path, eth).supply(USDC, 5.0)

    status, _, cause = await _only_intent(store)
    assert status == "failed"
    assert cause is not None
    assert "ConnectionResetError" in cause
    assert (await store.latency_stats("path.aave_supply"))["count"] == 0


@pytest.mark.asyncio
async def test_a_live_executor_without_a_key_fails_before_sending_anything(
    config: Config, store: StateStore, tmp_path: Path
) -> None:
    eth = _Eth(_Call(), {"status": 1})
    executor = _live_executor(config, store, tmp_path, eth, private_key=None)
    with pytest.raises(NotImplementedError, match="private key"):
        await executor.supply(USDC, 5.0)

    assert eth.sent == []
    status, _, _ = await _only_intent(store)
    assert status == "failed"
