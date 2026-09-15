"""The Aave leg of a snapshot in one Multicall3 eth_call.

Written after the 2026-09-13 quota alert: the M1 run alone had spent ~80 % of
the free RPC allowance, eleven reads per cycle. The fake chain below decodes
the real aggregate3 calldata and dispatches each sub-call by target and
selector, so these tests exercise the actual encoding, not a mock of it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from eth_abi.abi import decode, encode
from eth_utils.abi import function_signature_to_4byte_selector
from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.providers.async_base import AsyncBaseProvider
from web3.types import RPCEndpoint, RPCResponse

from delta0.venues.aave import (
    _ADDRESSES_PROVIDER_ABI,
    _DATA_PROVIDER_ABI,
    _ERC20_BALANCE_ABI,
    _MULTICALL3_ABI,
    _ORACLE_ABI,
    _POOL_ABI,
    AaveReader,
    MulticallError,
)

_USER = "0x4F7e000000000000000000000000000000000C89"
_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
_MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
_WSTETH = "0x5979D7b546E38E414F7E9822514be443A4800529"
_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
_WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
_A_WSTETH = "0x0000000000000000000000000000000000000A01"
_D_WSTETH = "0x0000000000000000000000000000000000000D01"
_A_USDC = "0x0000000000000000000000000000000000000A02"
_D_USDC = "0x0000000000000000000000000000000000000D02"
_PROVIDER = "0x0000000000000000000000000000000000000B01"
_ORACLE = "0x0000000000000000000000000000000000000C01"
_DATA_PROVIDER = "0x0000000000000000000000000000000000000E01"

_AGGREGATE3 = function_signature_to_4byte_selector("aggregate3((address,bool,bytes)[])")
_RAY = 10**27


class _RevertError(Exception):
    pass


def _reserve(atoken: str, debt: str, liquidity_rate: int, borrow_rate: int) -> tuple[Any, ...]:
    zero = "0x" + "00" * 20
    return (0, 0, liquidity_rate, 0, borrow_rate, 0, 0, 0, atoken, zero, debt, zero, 0, 0, 0)


class _Chain:
    """A handful of contracts, answering from canned values."""

    def __init__(self) -> None:
        self._handlers: dict[
            tuple[str, bytes], tuple[list[str], list[str], Callable[..., Any]]
        ] = {}
        self.gas_wei = 27_723_837_823_519_067
        world: list[tuple[str, list[dict[str, Any]], str, Callable[..., Any]]] = [
            (
                _POOL,
                _POOL_ABI,
                "getUserAccountData",
                lambda _u: (
                    51_000 * 10**8,
                    35_000 * 10**8,
                    0,
                    7_900,
                    7_500,
                    1_151_100_000_000_000_000,
                ),
            ),
            (_POOL, _POOL_ABI, "getUserEMode", lambda _u: (0,)),
            (_POOL, _POOL_ABI, "ADDRESSES_PROVIDER", lambda: (_PROVIDER,)),
            (_PROVIDER, _ADDRESSES_PROVIDER_ABI, "getPriceOracle", lambda: (_ORACLE,)),
            (_ORACLE, _ORACLE_ABI, "BASE_CURRENCY_UNIT", lambda: (10**8,)),
            (
                _ORACLE,
                _ORACLE_ABI,
                "getAssetPrice",
                lambda asset: (
                    3_125 * 10**8 if asset.lower() == _WSTETH.lower() else 2_500 * 10**8,
                ),
            ),
            (
                _POOL,
                _POOL_ABI,
                "getReserveData",
                lambda asset: (
                    _reserve(_A_WSTETH, _D_WSTETH, 0, 0)
                    if asset.lower() == _WSTETH.lower()
                    else _reserve(_A_USDC, _D_USDC, 27 * _RAY // 1000, 365 * _RAY // 10_000)
                ),
            ),
            (_MULTICALL, _MULTICALL3_ABI, "getEthBalance", lambda _u: (self.gas_wei,)),
            (
                # Arbitrum's real wstETH reserve, read on-chain: LTV max 0.75, LT 0.79.
                _DATA_PROVIDER,
                _DATA_PROVIDER_ABI,
                "getReserveConfigurationData",
                lambda _asset: (18, 7_500, 7_900, 10_720, 1_500, True, False, False, True, False),
            ),
        ]
        balances = {
            _A_WSTETH: (18, 16 * 10**18),
            _D_WSTETH: (18, 0),
            _WSTETH: (18, 0),
            _A_USDC: (6, 1_000 * 10**6),
            _D_USDC: (6, 35_000 * 10**6),
            _USDC: (6, 143_007_588),
        }
        for token, (dec, bal) in balances.items():
            world.append((token, _ERC20_BALANCE_ABI, "decimals", lambda d=dec: (d,)))
            world.append((token, _ERC20_BALANCE_ABI, "balanceOf", lambda _u, b=bal: (b,)))
        for address, abi, name, fn in world:
            self.on(address, abi, name, fn)

    def on(
        self, address: str, abi: list[dict[str, Any]], name: str, fn: Callable[..., Any]
    ) -> None:
        entry = next(e for e in abi if e["name"] == name)
        in_types = [i["type"] for i in entry["inputs"]]
        out_types = [o["type"] for o in entry["outputs"]]
        selector = function_signature_to_4byte_selector(f"{name}({','.join(in_types)})")
        self._handlers[(address.lower(), selector)] = (in_types, out_types, fn)

    def forget(self, address: str, abi: list[dict[str, Any]], name: str) -> None:
        """Leave `address.name` without code: the call succeeds and returns nothing."""
        entry = next(e for e in abi if e["name"] == name)
        in_types = [i["type"] for i in entry["inputs"]]
        selector = function_signature_to_4byte_selector(f"{name}({','.join(in_types)})")
        del self._handlers[(address.lower(), selector)]

    def execute(self, to: str, data: bytes) -> bytes:
        if to.lower() == _MULTICALL.lower() and data[:4] == _AGGREGATE3:
            (calls,) = decode(["(address,bool,bytes)[]"], data[4:])
            results: list[tuple[bool, bytes]] = []
            for target, allow_failure, calldata in calls:
                try:
                    results.append((True, self.execute(target, calldata)))
                except _RevertError:
                    if not allow_failure:
                        raise
                    results.append((False, b""))
            return encode(["(bool,bytes)[]"], [results])
        handler = self._handlers.get((to.lower(), data[:4]))
        if handler is None:
            return b""
        in_types, out_types, fn = handler
        return encode(out_types, list(fn(*decode(in_types, data[4:]))))


class _FakeProvider(AsyncBaseProvider):
    def __init__(self, chain: _Chain) -> None:
        super().__init__()
        self.chain = chain
        self.requests: list[str] = []

    async def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        self.requests.append(str(method))
        if method == "eth_chainId":
            result: Any = "0xa4b1"
        elif method == "eth_getBalance":
            result = hex(self.chain.gas_wei)
        elif method == "eth_call":
            tx = params[0]
            try:
                result = "0x" + self.chain.execute(tx["to"], bytes(HexBytes(tx["data"]))).hex()
            except _RevertError:
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 3, "message": "execution reverted"},
                }
        else:
            raise AssertionError(f"méthode inattendue {method}")
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    async def is_connected(self, show_traceback: bool = False) -> bool:
        _ = show_traceback
        return True


def _reader(
    chain: _Chain,
    *,
    multicall: str | None = _MULTICALL,
    data_provider: str | None = _DATA_PROVIDER,
) -> tuple[AaveReader, _FakeProvider]:
    provider = _FakeProvider(chain)
    reader = AaveReader(
        AsyncWeb3(provider),
        _POOL,
        _USER,
        multicall_address=multicall,
        data_provider_address=data_provider,
    )
    return reader, provider


async def _snapshot(reader: AaveReader) -> Any:
    return await reader.read_snapshot(
        wsteth_address=_WSTETH, usdc_address=_USDC, weth_address=_WETH
    )


@pytest.mark.asyncio
async def test_batched_snapshot_reads_the_numbers_the_single_reads_read() -> None:
    # The single reads stay for the status panel and the balance guard. If the
    # two paths ever disagreed, the bot would decide on one number and show another.
    reader, _ = _reader(_Chain())
    reads = await _snapshot(reader)

    assert reads.account == await reader.read_account_data()
    assert reads.wsteth == await reader.read_token_balances(_WSTETH)
    assert reads.usdc == await reader.read_token_balances(_USDC)
    assert reads.usdc_rates == await reader.read_reserve_rates(_USDC)
    assert reads.oracle == await reader.read_oracle_prices(_WSTETH, _WETH)
    assert reads.gas_eth == await reader.read_gas_balance_eth()

    # And the conversions land on the reference world.
    assert reads.account.health_factor == pytest.approx(1.1511)
    assert reads.account.liquidation_threshold == pytest.approx(0.79)
    assert reads.wsteth.atoken_balance == pytest.approx(16.0)
    assert reads.usdc.variable_debt_balance == pytest.approx(35_000.0)
    assert reads.usdc.wallet_balance == pytest.approx(143.007588)
    assert reads.usdc_rates.variable_borrow_apr == pytest.approx(0.0365)
    assert reads.oracle.wsteth_eth_ratio == pytest.approx(1.25)
    assert reads.gas_eth == pytest.approx(0.027723837823519067)


@pytest.mark.asyncio
async def test_a_warm_snapshot_costs_one_eth_call() -> None:
    reader, provider = _reader(_Chain())
    await _snapshot(reader)  # first cycle resolves token metadata and the oracle

    provider.requests.clear()
    await _snapshot(reader)

    paid = [m for m in provider.requests if m != "eth_chainId"]
    assert paid == ["eth_call"]


@pytest.mark.asyncio
async def test_a_reverting_sub_call_is_named() -> None:
    chain = _Chain()
    reader, _ = _reader(chain)
    await _snapshot(reader)

    def revert(_asset: str) -> tuple[int]:
        raise _RevertError

    chain.on(_ORACLE, _ORACLE_ABI, "getAssetPrice", revert)
    with pytest.raises(MulticallError, match="prix oracle wstETH, prix oracle WETH"):
        await _snapshot(reader)


@pytest.mark.asyncio
async def test_a_sub_call_answering_nothing_is_named() -> None:
    # A call to an address without code succeeds with empty data. Decoding it
    # as zero would report an empty wallet; it must fail instead.
    chain = _Chain()
    reader, _ = _reader(chain)
    await _snapshot(reader)

    chain.forget(_USDC, _ERC20_BALANCE_ABI, "balanceOf")
    with pytest.raises(MulticallError, match="solde libre USDC"):
        await _snapshot(reader)


@pytest.mark.asyncio
async def test_a_multicall_address_without_code_is_refused() -> None:
    reader, _ = _reader(_Chain(), multicall="0x000000000000000000000000000000000000dEaD")
    with pytest.raises(MulticallError, match="aucun contrat"):
        await _snapshot(reader)


@pytest.mark.asyncio
async def test_a_reader_without_multicall_refuses_the_batch() -> None:
    reader, provider = _reader(_Chain(), multicall=None)
    with pytest.raises(MulticallError, match="aucune adresse Multicall3"):
        await _snapshot(reader)
    assert provider.requests == []


# --- The liquidation threshold of an empty account ----------------------------------
#
# Aave reports LT 0 for an account without collateral. The bands check refused
# that — rightly — which kept `delta0 tracer` from starting at all on the
# operator's account, empty since the M1 position was closed (2026-09-14).


def _empty_account(chain: _Chain) -> None:
    max_uint = 2**256 - 1  # Aave's health factor for an account without debt
    chain.on(_POOL, _POOL_ABI, "getUserAccountData", lambda _u: (0, 0, 0, 0, 0, max_uint))


@pytest.mark.asyncio
async def test_an_empty_account_takes_the_reserve_threshold() -> None:
    chain = _Chain()
    _empty_account(chain)
    reader, _ = _reader(chain)
    reads = await _snapshot(reader)
    assert reads.account.liquidation_threshold == pytest.approx(0.79)
    assert reads.account.ltv_max == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_an_open_position_keeps_the_account_threshold() -> None:
    """The account's LT is collateral-weighted (the USDC cushion counts): it wins."""
    chain = _Chain()
    chain.on(
        _POOL,
        _POOL_ABI,
        "getUserAccountData",
        lambda _u: (51_000 * 10**8, 35_000 * 10**8, 0, 7_880, 7_480, 1_148_000_000_000_000_000),
    )
    reader, _ = _reader(chain)
    reads = await _snapshot(reader)
    assert reads.account.liquidation_threshold == pytest.approx(0.788)
    assert reads.account.ltv_max == pytest.approx(0.748)


@pytest.mark.asyncio
async def test_without_a_data_provider_an_empty_account_still_reads_zero() -> None:
    """No silent default: the boot check then refuses, with a message."""
    chain = _Chain()
    _empty_account(chain)
    reader, _ = _reader(chain, data_provider=None)
    reads = await _snapshot(reader)
    assert reads.account.liquidation_threshold == 0.0
