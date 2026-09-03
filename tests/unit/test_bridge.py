"""BridgeExecutor — dry-run and mocked live paths."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from delta0.config import Config
from delta0.safety import MicroOpsGuard, SafetyRefused
from delta0.state import StateStore
from delta0.venues.bridge import (
    MIN_DEPOSIT_USDC,
    MIN_WITHDRAW_USDC,
    BridgeExecutor,
    BridgeLegResult,
)

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
BRIDGE2 = "0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


class _FakeCall:
    def __init__(self, value: object) -> None:
        self._value = value

    async def call(self) -> object:
        return self._value


class _FakeUsdc:
    def __init__(self) -> None:
        self.address = USDC

    class functions:  # noqa: N801 - mimic web3 API
        @staticmethod
        def decimals() -> _FakeCall:
            return _FakeCall(6)

        @staticmethod
        def balanceOf(_addr: str) -> _FakeCall:  # noqa: N802 - solidity name
            return _FakeCall(50_000_000)  # 50 USDC

        @staticmethod
        def transfer(_to: str, _amount: int) -> MagicMock:
            return MagicMock()


class _FakeEth:
    def contract(self, address: str, abi: object) -> _FakeUsdc:
        _ = abi
        _ = address
        return _FakeUsdc()


def _dummy_leg() -> BridgeLegResult:
    return BridgeLegResult(
        intent_id="test",
        status="confirmed",
        tx_hash=None,
        duration_ms=1.0,
    )


def _make_bridge(
    tmp_path: Path,
    store: StateStore,
    config: Config,
    *,
    dry_run: bool = True,
    require_confirm: bool = False,
) -> BridgeExecutor:
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={
                    "dry_run": dry_run,
                    "require_first_use_confirmation": require_confirm,
                    "max_op_usd": 100.0,  # tests use amounts up to ~10
                },
            ),
        },
    )
    guard = MicroOpsGuard(config=cfg.tracer, project_root=tmp_path)
    if not require_confirm:
        guard.confirm_kind("bridge_out")
        guard.confirm_kind("bridge_in")

    w3 = MagicMock()
    w3.eth = _FakeEth()
    hl_exchange = MagicMock()
    hl_info = MagicMock()
    hl_info.user_state.return_value = {"marginSummary": {"accountValue": "0"}}

    return BridgeExecutor(
        web3=w3,
        config=cfg,
        store=store,
        guard=guard,
        master_address="0x000000000000000000000000000000000000dEaD",
        chain_id=42161,
        hl_exchange_factory=lambda: hl_exchange,
        hl_info=hl_info,
    )


@pytest.mark.asyncio
async def test_bridge_out_dry_run(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    result = await bridge.bridge_out(amount_usdc=10.0)
    assert result.status == "dry_run"
    assert result.tx_hash is None
    stats = await store.latency_stats("dry.path.bridge_out_submit")
    assert stats["count"] == 1


@pytest.mark.asyncio
async def test_bridge_in_dry_run(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    result = await bridge.bridge_in(amount_usdc=10.0)
    assert result.status == "dry_run"
    stats = await store.latency_stats("dry.path.bridge_in_submit")
    assert stats["count"] == 1


@pytest.mark.asyncio
async def test_bridge_out_below_minimum_refused(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    with pytest.raises(ValueError, match="minimum"):
        await bridge.bridge_out(amount_usdc=MIN_DEPOSIT_USDC - 0.01)


@pytest.mark.asyncio
async def test_bridge_in_below_minimum_refused(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    with pytest.raises(ValueError, match="minimum"):
        await bridge.bridge_in(amount_usdc=MIN_WITHDRAW_USDC - 0.01)


@pytest.mark.asyncio
async def test_bridge_out_kill_file_blocks(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    (tmp_path / "KILL").write_text("")
    with pytest.raises(SafetyRefused, match="KILL"):
        await bridge.bridge_out(amount_usdc=10.0)


@pytest.mark.asyncio
async def test_bridge_in_first_use_gate(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True, require_confirm=True)
    with pytest.raises(SafetyRefused, match="confirmation"):
        await bridge.bridge_in(amount_usdc=10.0)


@pytest.mark.asyncio
async def test_round_trip_dry_run(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    result = await bridge.round_trip(amount_usdc=10.0)
    assert result.up.status == "dry_run"
    assert result.down.status == "dry_run"
    # In dry-run the credit waits are skipped.
    assert result.up_credit_wait_ms == 0.0
    assert result.down_credit_wait_ms == 0.0


@pytest.mark.asyncio
async def test_live_send_path_requires_pkey_override(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=False)
    with pytest.raises(NotImplementedError, match="private key"):
        bridge._pkey()  # deliberate probe of the safety hook


# --- HL balance reads spot AND perp -------------------------------------------
#
# The first live bridge round trip timed out after 900 s waiting for a credit
# that had already landed: USDC bridged from Arbitrum is credited to the SPOT
# sub-account, and the poll only looked at perp.


def test_perp_usdc_reads_the_margin_summary() -> None:
    assert BridgeExecutor._perp_usdc({"marginSummary": {"accountValue": "24.8"}}) == 24.8


def test_spot_usdc_picks_usdc_out_of_the_balances() -> None:
    state = {
        "balances": [
            {"coin": "USDE", "total": "0.0"},
            {"coin": "USDC", "total": "29.8", "hold": "0.0"},
        ],
    }
    assert BridgeExecutor._spot_usdc(state) == 29.8


@pytest.mark.parametrize(
    "state",
    [None, {}, {"balances": None}, {"balances": []}, {"balances": [{"coin": "USDT0"}]}],
)
def test_spot_usdc_is_zero_when_absent(state: object) -> None:
    """A malformed or USDC-less response must read as zero, never crash.

    This runs inside a polling loop during a live bridge crossing; an exception
    here would abort a round trip with real funds mid-flight.
    """
    assert BridgeExecutor._spot_usdc(state) == 0.0


@pytest.mark.parametrize(
    "state",
    [None, {}, {"marginSummary": None}, {"marginSummary": {}}],
)
def test_perp_usdc_is_zero_when_absent(state: object) -> None:
    assert BridgeExecutor._perp_usdc(state) == 0.0


def test_bridge_credit_is_seen_when_funds_land_in_spot() -> None:
    """The exact shape that broke the first live crossing: perp 0, spot funded."""
    perp: dict[str, object] = {"marginSummary": {"accountValue": "0.0"}}
    spot = {"balances": [{"coin": "USDC", "total": "29.8"}]}
    total = BridgeExecutor._perp_usdc(perp) + BridgeExecutor._spot_usdc(spot)
    assert total == 29.8


# --- spot -> perp reconciliation ----------------------------------------------
#
# `bridge_out` credits HL's SPOT sub-account; `bridge_in` (`withdraw3`) draws on
# PERP. Without a transfer between them each round trip drains perp by the trip
# amount — 70 USDC over the 14 crossings of a 7-day run, breaking the withdrawal
# around the fifth and the HL orders along with it.


@pytest.mark.asyncio
async def test_round_trip_moves_bridged_usdc_to_perp(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    bridge = _make_bridge(tmp_path, store, config, dry_run=False)
    exchange = bridge._make_hl_exchange()

    # Stub the chain legs and the credit waits: this test is about the order of
    # the sub-account bookkeeping, not about the crossings themselves.
    bridge.bridge_out = AsyncMock(return_value=_dummy_leg())  # type: ignore[method-assign]
    bridge.bridge_in = AsyncMock(return_value=_dummy_leg())  # type: ignore[method-assign]
    bridge.wait_for_hl_credit = AsyncMock(return_value=1000.0)  # type: ignore[method-assign]
    bridge.wait_for_arbitrum_credit = AsyncMock(return_value=2000.0)  # type: ignore[method-assign]

    await bridge.round_trip(5.0)

    exchange.usd_class_transfer.assert_called_once_with(5.0, True)


@pytest.mark.asyncio
async def test_dry_run_never_transfers(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """A rehearsal must not touch the sub-accounts any more than the chain."""
    bridge = _make_bridge(tmp_path, store, config, dry_run=True)
    exchange = bridge._make_hl_exchange()

    await bridge.round_trip(5.0)

    exchange.usd_class_transfer.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_transfer_does_not_abort_the_crossing(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """Funds are already on HL at this point — aborting would strand them."""
    bridge = _make_bridge(tmp_path, store, config, dry_run=False)
    exchange = bridge._make_hl_exchange()
    exchange.usd_class_transfer.side_effect = RuntimeError("HL a refuse")

    bridge.bridge_out = AsyncMock(return_value=_dummy_leg())  # type: ignore[method-assign]
    bridge.bridge_in = AsyncMock(return_value=_dummy_leg())  # type: ignore[method-assign]
    bridge.wait_for_hl_credit = AsyncMock(return_value=1000.0)  # type: ignore[method-assign]
    bridge.wait_for_arbitrum_credit = AsyncMock(return_value=2000.0)  # type: ignore[method-assign]

    result = await bridge.round_trip(5.0)

    assert result.down_credit_wait_ms == 2000.0
    bridge.bridge_in.assert_awaited_once()
