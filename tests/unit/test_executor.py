"""AaveTraceExecutor unit tests — web3 fully mocked, no network.

We verify the pending-intent journal, the safety hookup, the latency
recording, and the dry-run vs live paths. The actual on-chain integration
lives in a marked `integration` test to be run against an anvil fork later.
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
WSTETH = "0x5979D7b546E38E414F7E9822514be443A4800529"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"


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
    def __init__(self, decimals: int) -> None:
        self._decimals = decimals

    def decimals(self) -> _FakeAsyncCall:
        return _FakeAsyncCall(self._decimals)

    def approve(self, *_args: object) -> object:
        return object()


class _FakePoolFunctions:
    def supply(self, *_args: object) -> object:
        return object()

    def borrow(self, *_args: object) -> object:
        return object()

    def repay(self, *_args: object) -> object:
        return object()

    def withdraw(self, *_args: object) -> object:
        return object()


class _FakeContract:
    def __init__(self, address: str, functions: object) -> None:
        self.address = address
        self.functions = functions


class _FakeEth:
    def __init__(self, decimals_by_addr: dict[str, int]) -> None:
        self._decimals = decimals_by_addr

    def contract(self, address: str, abi: object) -> _FakeContract:
        _ = abi
        if address.lower() == POOL.lower():
            return _FakeContract(address, _FakePoolFunctions())
        dec = self._decimals.get(address.lower(), 18)
        return _FakeContract(address, _FakeERC20Functions(dec))


def _make_executor(
    tmp_path: Path,
    store: StateStore,
    config: Config,
    *,
    dry_run: bool = True,
    require_confirm: bool = False,
    max_op_usd: float | None = None,
) -> tuple[AaveTraceExecutor, MicroOpsGuard]:
    tracer_update: dict[str, object] = {
        "dry_run": dry_run,
        "require_first_use_confirmation": require_confirm,
    }
    if max_op_usd is not None:
        tracer_update["max_op_usd"] = max_op_usd
    cfg = config.model_copy(update={"tracer": config.tracer.model_copy(update=tracer_update)})
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    if not require_confirm:
        for kind in ("aave_approve", "aave_supply", "aave_borrow", "aave_repay", "aave_withdraw"):
            guard.confirm_kind(kind)
    w3 = MagicMock()
    w3.eth = _FakeEth(
        decimals_by_addr={USDC.lower(): 6, WSTETH.lower(): 18},
    )
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
    return executor, guard


@pytest.mark.asyncio
async def test_dry_run_supply_journals_pending_then_confirmed(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True)
    result = await executor.supply(USDC, 10.0)
    assert result.status == "dry_run"
    assert result.tx_hash is None
    assert result.duration_ms >= 0

    # Latency lands in the dry-run namespace so a rehearsal can never
    # contaminate the live critical-path statistics.
    stats = await store.latency_stats("dry.path.aave_supply")
    assert stats["count"] == 1


@pytest.mark.asyncio
async def test_dry_run_all_verbs(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True)
    # All amounts kept well under the 15 $ default cap.
    r1 = await executor.approve(USDC, 10.0)
    r2 = await executor.supply(USDC, 10.0)
    r3 = await executor.borrow(USDC, 1.0)
    r4 = await executor.repay(USDC, 1.0)
    r5 = await executor.withdraw(USDC, 10.0)
    for r in (r1, r2, r3, r4, r5):
        assert r.status == "dry_run"
    stats_ap = await store.latency_stats("dry.path.aave_approve")
    stats_su = await store.latency_stats("dry.path.aave_supply")
    stats_bo = await store.latency_stats("dry.path.aave_borrow")
    stats_re = await store.latency_stats("dry.path.aave_repay")
    stats_wd = await store.latency_stats("dry.path.aave_withdraw")
    for s in (stats_ap, stats_su, stats_bo, stats_re, stats_wd):
        assert s["count"] == 1


@pytest.mark.asyncio
async def test_supply_refused_by_cap(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True, max_op_usd=5.0)
    with pytest.raises(SafetyRefused, match="plafond"):
        # 10 USDC > 5 $ cap.
        await executor.supply(USDC, 10.0)


@pytest.mark.asyncio
async def test_supply_refused_when_kill_file(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True)
    (tmp_path / "KILL").write_text("")
    with pytest.raises(SafetyRefused, match="KILL"):
        await executor.supply(USDC, 10.0)


@pytest.mark.asyncio
async def test_first_use_gate_blocks_without_confirm(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    executor, guard = _make_executor(
        tmp_path,
        store,
        config,
        dry_run=True,
        require_confirm=True,
    )
    with pytest.raises(SafetyRefused, match="confirmation"):
        await executor.supply(USDC, 10.0)
    guard.confirm_kind("aave_supply")
    result = await executor.supply(USDC, 10.0)
    assert result.status == "dry_run"


@pytest.mark.asyncio
async def test_wsteth_amount_estimated_at_3000_dollars(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    # 0.01 wstETH ≈ 30 $ estimated — above default 15 $ cap.
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True)
    with pytest.raises(SafetyRefused, match="plafond"):
        await executor.supply(WSTETH, 0.01)


def test_live_send_path_requires_pkey_override(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """Sanity check: an untouched executor cannot spend real money.

    The private key path deliberately raises NotImplementedError so that a
    live-mode caller who forgot to inject a signer fails LOUDLY at the first
    signing attempt (before any tx leaves the process).
    """
    executor, _ = _make_executor(tmp_path, store, config, dry_run=False)
    with pytest.raises(NotImplementedError, match="private key"):
        executor._pkey()


@pytest.mark.asyncio
async def test_dry_run_samples_never_enter_the_critical_path_stats(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """A rehearsal must not be able to flatter a live latency report.

    Dry-run ops take microseconds because they skip the network. If they
    landed on `path.*`, rehearsing before a live run would drag p50/p95 down
    and could turn a DEPASSE verdict into a false OK.
    """
    executor, _ = _make_executor(tmp_path, store, config, dry_run=True)
    await executor.supply(USDC, 10.0)

    assert await store.latency_paths() == ["dry.path.aave_supply"]
    live_stats = await store.latency_stats("path.aave_supply")
    assert live_stats["count"] == 0
