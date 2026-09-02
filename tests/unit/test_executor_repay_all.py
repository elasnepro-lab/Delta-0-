"""AaveTraceExecutor closes Aave round trips with the MAX_UINT256 sentinel.

Both ends of the cycle need it: `repay_all` because a partial repay leaves
accrued interest, `withdraw_all` because Aave's scaled-balance rounding can
leave the aToken one unit below the deposit. See memory/aave_findings.md.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delta0.config import Config
from delta0.executor import AaveTraceExecutor
from delta0.safety import MicroOpsGuard, SafetyRefused
from delta0.state import StateStore

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


class _FakeAsyncCall:
    def __init__(self, result: object) -> None:
        self._result = result

    async def call(self) -> object:
        return self._result


class _FakeERC20Functions:
    def decimals(self) -> _FakeAsyncCall:
        return _FakeAsyncCall(6)


class _FakePoolFunctions:
    def __init__(self) -> None:
        self.last_repay_args: tuple[object, ...] | None = None
        self.last_withdraw_args: tuple[object, ...] | None = None

    def repay(self, *args: object) -> object:
        self.last_repay_args = args
        return object()

    def withdraw(self, *args: object) -> object:
        self.last_withdraw_args = args
        return object()


class _FakeContract:
    def __init__(self, address: str, functions: object) -> None:
        self.address = address
        self.functions = functions


class _FakeEth:
    def __init__(self, pool_addr: str) -> None:
        self._pool_addr = pool_addr
        self._pool_functions = _FakePoolFunctions()

    def contract(self, address: str, abi: object) -> _FakeContract:
        _ = abi
        if address.lower() == self._pool_addr.lower():
            return _FakeContract(address, self._pool_functions)
        return _FakeContract(address, _FakeERC20Functions())


@pytest.mark.asyncio
async def test_repay_all_uses_max_uint256(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={"dry_run": True, "require_first_use_confirmation": False},
            ),
        },
    )
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    for kind in ("aave_repay",):
        guard.confirm_kind(kind)
    w3 = MagicMock()
    fake_eth = _FakeEth(cfg.venues.aave_pool)
    w3.eth = fake_eth

    with patch("delta0.executor.AsyncWeb3") as async_web3_mock:
        async_web3_mock.to_checksum_address.side_effect = lambda a: a
        executor = AaveTraceExecutor(
            web3=w3,
            config=cfg,
            store=store,
            guard=guard,
            master_address="0x000000000000000000000000000000000000dEaD",
            chain_id=42161,
        )
        result = await executor.repay_all(USDC)

    assert result.status == "dry_run"
    # The pool.repay call was built with MAX_UINT256.
    max_uint = 2**256 - 1
    assert fake_eth._pool_functions.last_repay_args is not None
    call_args = fake_eth._pool_functions.last_repay_args
    # (asset, amount, rate_mode, on_behalf_of)
    assert call_args[1] == max_uint


@pytest.mark.asyncio
async def test_withdraw_all_uses_max_uint256(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """The exact-amount withdraw reverts on rounding; the sentinel does not."""
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={"dry_run": True, "require_first_use_confirmation": False},
            ),
        },
    )
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    guard.confirm_kind("aave_withdraw")
    w3 = MagicMock()
    fake_eth = _FakeEth(cfg.venues.aave_pool)
    w3.eth = fake_eth

    with patch("delta0.executor.AsyncWeb3") as async_web3_mock:
        async_web3_mock.to_checksum_address.side_effect = lambda a: a
        executor = AaveTraceExecutor(
            web3=w3,
            config=cfg,
            store=store,
            guard=guard,
            master_address="0x000000000000000000000000000000000000dEaD",
            chain_id=42161,
        )
        result = await executor.withdraw_all(USDC, 5.0)

    assert result.status == "dry_run"
    call_args = fake_eth._pool_functions.last_withdraw_args
    assert call_args is not None
    # (asset, amount, to)
    assert call_args[1] == 2**256 - 1


@pytest.mark.asyncio
async def test_withdraw_all_notional_hint_respects_the_amount_cap(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """MAX_UINT256 must never reach the guard as a notional — the hint does."""
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={
                    "dry_run": True,
                    "require_first_use_confirmation": False,
                    "max_op_usd": 10.0,
                },
            ),
        },
    )
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    guard.confirm_kind("aave_withdraw")
    w3 = MagicMock()
    w3.eth = _FakeEth(cfg.venues.aave_pool)

    with patch("delta0.executor.AsyncWeb3") as async_web3_mock:
        async_web3_mock.to_checksum_address.side_effect = lambda a: a
        executor = AaveTraceExecutor(
            web3=w3,
            config=cfg,
            store=store,
            guard=guard,
            master_address="0x000000000000000000000000000000000000dEaD",
            chain_id=42161,
        )
        with pytest.raises(SafetyRefused):
            await executor.withdraw_all(USDC, 50.0)
