"""The wallet balance check that was missing during the marche à blanc.

The tracer deposited 5 USDC per cycle without ever taking them back. Once the
wallet was empty, 86 supplies and 35 repays failed on-chain in a row, each one
paying gas to learn what a single read would have said for free. This guard
turns those failures into refusals.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delta0.config import Config
from delta0.executor import AaveTraceExecutor
from delta0.safety import InsufficientBalance, MicroOpsGuard, SafetyRefused
from delta0.state import StateStore
from delta0.venues.aave import AaveTokenBalances

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

    def approve(self, *args: object) -> object:
        return object()


class _FakePoolFunctions:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def supply(self, *args: object) -> object:
        self.calls.append("supply")
        return object()

    def repay(self, *args: object) -> object:
        self.calls.append("repay")
        return object()


class _FakeContract:
    def __init__(self, address: str, functions: object) -> None:
        self.address = address
        self.functions = functions


class _FakeEth:
    def __init__(self, pool_addr: str) -> None:
        self._pool_addr = pool_addr
        self.pool_functions = _FakePoolFunctions()

    def contract(self, address: str, abi: object) -> _FakeContract:
        _ = abi
        if address.lower() == self._pool_addr.lower():
            return _FakeContract(address, self.pool_functions)
        return _FakeContract(address, _FakeERC20Functions())


class _Balances:
    """Reader double. `wallet` is the number the guard is there to read."""

    def __init__(self, wallet: float, debt: float = 0.0) -> None:
        self._wallet = wallet
        self._debt = debt
        self.reads = 0

    async def read_token_balances(self, asset: str) -> AaveTokenBalances:
        self.reads += 1
        return AaveTokenBalances(
            atoken_balance=0.0,
            variable_debt_balance=self._debt,
            wallet_balance=self._wallet,
        )


class _Unreadable:
    """Reader whose RPC is down."""

    async def read_token_balances(self, asset: str) -> AaveTokenBalances:
        raise ConnectionError("rpc down")


def _executor(
    cfg: Config,
    store: StateStore,
    tmp_path: Path,
    balances: object | None,
) -> tuple[AaveTraceExecutor, _FakeEth]:
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    for kind in ("aave_supply", "aave_repay"):
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
            balances=balances,  # type: ignore[arg-type]
        )
    return executor, fake_eth


@pytest.fixture
def dry_config(config: Config) -> Config:
    return config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={"dry_run": True, "require_first_use_confirmation": False},
            ),
        },
    )


@pytest.mark.asyncio
async def test_a_funded_wallet_lets_the_supply_through(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, eth = _executor(dry_config, store, tmp_path, _Balances(wallet=10.0))
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        result = await executor.supply(USDC, 5.0)
    assert result.status == "dry_run"
    assert eth.pool_functions.calls == ["supply"]


@pytest.mark.asyncio
async def test_an_empty_wallet_refuses_the_supply_before_any_transaction(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """The exact shape of the 86 failures: 2,97 in the wallet, 5 asked for."""
    executor, eth = _executor(dry_config, store, tmp_path, _Balances(wallet=2.973592))
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        with pytest.raises(InsufficientBalance) as excinfo:
            await executor.supply(USDC, 5.0)
    # Refused BEFORE the call is even built: no gas, no receipt to interpret.
    assert eth.pool_functions.calls == []
    assert "2.973592" in str(excinfo.value)
    assert "5.000000" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_refusal_is_catchable_as_a_guard_refusal(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """The tracer already catches `SafetyRefused` and keeps the loop alive."""
    executor, _ = _executor(dry_config, store, tmp_path, _Balances(wallet=0.0))
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        with pytest.raises(SafetyRefused):
            await executor.supply(USDC, 5.0)


@pytest.mark.asyncio
async def test_repay_all_is_measured_against_the_debt_not_the_amount(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """MAX_UINT256 does not mean "whatever I have".

    Aave pulls the full outstanding debt, so that is what the wallet must
    hold. The 8 September numbers: 2,97 free against 35,01 owed.
    """
    executor, eth = _executor(
        dry_config,
        store,
        tmp_path,
        _Balances(wallet=2.973592, debt=35.012470),
    )
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        with pytest.raises(InsufficientBalance) as excinfo:
            await executor.repay_all(USDC)
    assert eth.pool_functions.calls == []
    assert "35.012470" in str(excinfo.value)


@pytest.mark.asyncio
async def test_repay_all_proceeds_when_the_wallet_covers_the_debt(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, eth = _executor(
        dry_config,
        store,
        tmp_path,
        _Balances(wallet=40.0, debt=35.012470),
    )
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        result = await executor.repay_all(USDC)
    assert result.status == "dry_run"
    assert eth.pool_functions.calls == ["repay"]


@pytest.mark.asyncio
async def test_no_debt_needs_no_funds(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """A repay with nothing owed is Aave's own revert to raise, not ours."""
    executor, _ = _executor(dry_config, store, tmp_path, _Balances(wallet=0.0, debt=0.0))
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        result = await executor.repay_all(USDC)
    assert result.status == "dry_run"


@pytest.mark.asyncio
async def test_an_unreadable_balance_does_not_block_the_operation(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """We block what we know to be impossible, not what we failed to check.

    On an emergency path a repay that might have worked is worth more than a
    refusal based on ignorance, and the transaction still carries its own
    revert as a second line of defence.
    """
    executor, eth = _executor(dry_config, store, tmp_path, _Unreadable())
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        result = await executor.supply(USDC, 5.0)
    assert result.status == "dry_run"
    assert eth.pool_functions.calls == ["supply"]


@pytest.mark.asyncio
async def test_no_reader_wired_does_not_block_either(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, eth = _executor(dry_config, store, tmp_path, None)
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        result = await executor.supply(USDC, 5.0)
    assert result.status == "dry_run"
    assert eth.pool_functions.calls == ["supply"]


@pytest.mark.asyncio
async def test_the_guard_costs_one_read_per_operation(
    dry_config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """One round trip, not two: the debt and the balance come together."""
    balances = _Balances(wallet=40.0, debt=1.0)
    executor, _ = _executor(dry_config, store, tmp_path, balances)
    with patch("delta0.executor.AsyncWeb3") as m:
        m.to_checksum_address.side_effect = lambda a: a
        await executor.repay_all(USDC)
    assert balances.reads == 1
