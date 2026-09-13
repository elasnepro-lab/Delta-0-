"""Aave v3 read-only wrapper (M0).

Provides:
- `read_snapshot`: everything a cycle needs, in ONE Multicall3 eth_call.
- `read_account_data`: HF, LTV, collateral, debt via `Pool.getUserAccountData`.
- `read_token_balances`: supplied, owed and free balances of one asset.
- `read_reserve_rates`: supply and variable-borrow APRs.
- `read_oracle_prices`: wstETH and WETH as the Aave oracle prices them.

The single reads stay for one-off callers (status panel, balance guard,
unwind script). The cycle goes through `read_snapshot`, and both paths share
the same conversions so they cannot disagree on a number.

No writes. No approvals. No mutations. That is the point of M0.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_typing import ChecksumAddress
from eth_utils.abi import function_signature_to_4byte_selector
from web3 import AsyncWeb3

from delta0.logging import get_logger

if TYPE_CHECKING:
    from web3.contract import AsyncContract

log = get_logger(__name__)

# Minimal ABIs — we only call view functions here.
_POOL_ABI: list[dict[str, Any]] = [
    {
        "name": "getUserAccountData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
    {
        "name": "ADDRESSES_PROVIDER",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "getUserEMode",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            # Simplified: we only need the ATokenAddress and variableDebtTokenAddress.
            # Aave returns a struct; web3.py will decode as a tuple. We index by position.
            # Every member is static, so the struct is ABI-encoded exactly like
            # this flat list — which is what lets the multicall path decode it.
            {"name": "configuration", "type": "uint256"},
            {"name": "liquidityIndex", "type": "uint128"},
            {"name": "currentLiquidityRate", "type": "uint128"},
            {"name": "variableBorrowIndex", "type": "uint128"},
            {"name": "currentVariableBorrowRate", "type": "uint128"},
            {"name": "currentStableBorrowRate", "type": "uint128"},
            {"name": "lastUpdateTimestamp", "type": "uint40"},
            {"name": "id", "type": "uint16"},
            {"name": "aTokenAddress", "type": "address"},
            {"name": "stableDebtTokenAddress", "type": "address"},
            {"name": "variableDebtTokenAddress", "type": "address"},
            {"name": "interestRateStrategyAddress", "type": "address"},
            {"name": "accruedToTreasury", "type": "uint128"},
            {"name": "unbacked", "type": "uint128"},
            {"name": "isolationModeTotalDebt", "type": "uint128"},
        ],
    },
]

_ERC20_BALANCE_ABI: list[dict[str, Any]] = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

# Aave scales collateral/debt in "base currency" with 8 decimals.
_BASE_DECIMALS = 10**8
# HF is returned in wad (1e18); infinity when there is no debt.
_HF_WAD = 10**18
# Interest rates are in RAY (1e27) and expressed as annualized values.
_RAY = 10**27


@dataclass(frozen=True, slots=True)
class AaveAccountData:
    total_collateral_usd: float
    total_debt_usd: float
    available_borrows_usd: float
    liquidation_threshold: float  # ratio (e.g. 0.83)
    ltv_max: float  # ratio
    health_factor: float  # infinity if no debt
    emode: int


@dataclass(frozen=True, slots=True)
class AaveTokenBalances:
    atoken_balance: float  # native units (float, tight to Decimal in M1)
    variable_debt_balance: float
    wallet_balance: float  # free in the wallet — what an operation spends


_ADDRESSES_PROVIDER_ABI: list[dict[str, Any]] = [
    {
        "name": "getPriceOracle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]

_ORACLE_ABI: list[dict[str, Any]] = [
    {
        "name": "getAssetPrice",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "BASE_CURRENCY_UNIT",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Multicall3 exposes the native balance too, so the gas reading rides in the
# same batch instead of costing its own eth_getBalance.
_MULTICALL3_ABI: list[dict[str, Any]] = [
    {
        "name": "getEthBalance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "addr", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}],
    },
]
_AGGREGATE3_SELECTOR = function_signature_to_4byte_selector("aggregate3((address,bool,bytes)[])")


class MulticallError(RuntimeError):
    """A batched read did not come back whole.

    Raised rather than returning partial numbers: a snapshot with one field
    silently zeroed is worse than no snapshot, because the watchdog counts the
    failure and the decision engine never sees the hole.
    """


@dataclass(frozen=True, slots=True)
class AaveOraclePrices:
    """Asset prices as Aave itself sees them, in its base currency (USD).

    These are the prices behind the health factor. Pricing the collateral with
    anything else — a perp mark, a DEX mid — means deciding on a different
    number from the one that can liquidate us.
    """

    wsteth_usd: float
    weth_usd: float

    @property
    def wsteth_eth_ratio(self) -> float:
        """How many ETH one wstETH is worth, per the oracle.

        `stEthPerToken()` does not exist on Arbitrum's bridged wstETH, so the
        rate comes from the ratio of two oracle prices. That is the better
        source anyway: it cannot drift from the prices Aave applies.
        """
        if self.weth_usd == 0.0:
            return 0.0
        return self.wsteth_usd / self.weth_usd


@dataclass(frozen=True, slots=True)
class AaveReserveRates:
    """Interest rates for one reserve (already converted from RAY to APR)."""

    liquidity_apr: float  # supply-side APR
    variable_borrow_apr: float  # variable borrow APR — the one used by the bot


@dataclass(frozen=True, slots=True)
class AaveSnapshotReads:
    """The Aave leg of one cycle, read at a single block."""

    account: AaveAccountData
    wsteth: AaveTokenBalances
    usdc: AaveTokenBalances
    usdc_rates: AaveReserveRates
    gas_eth: float
    oracle: AaveOraclePrices


@dataclass(frozen=True, slots=True)
class _TokenMeta:
    """Immutable per-asset metadata cached forever after first fetch."""

    atoken_address: ChecksumAddress
    var_debt_address: ChecksumAddress
    atoken_decimals: int
    var_debt_decimals: int


@dataclass(frozen=True, slots=True)
class _Call:
    """One view call inside a Multicall3 batch."""

    label: str  # named in the error when this call fails
    target: ChecksumAddress
    calldata: bytes
    out_types: tuple[str, ...]


def _call(
    label: str, target: ChecksumAddress, abi: list[dict[str, Any]], name: str, *args: Any
) -> _Call:
    """Encode `name(args)` from its ABI entry, so types are written only once."""
    fn = next(entry for entry in abi if entry["name"] == name)
    in_types = [i["type"] for i in fn["inputs"]]
    selector = function_signature_to_4byte_selector(f"{name}({','.join(in_types)})")
    return _Call(
        label=label,
        target=target,
        calldata=selector + abi_encode(in_types, list(args)),
        out_types=tuple(o["type"] for o in fn["outputs"]),
    )


def _account_data(account_tuple: tuple[int, ...], emode: int) -> AaveAccountData:
    (total_collateral, total_debt, available_borrows, lt, ltv_max, hf) = account_tuple
    return AaveAccountData(
        total_collateral_usd=total_collateral / _BASE_DECIMALS,
        total_debt_usd=total_debt / _BASE_DECIMALS,
        available_borrows_usd=available_borrows / _BASE_DECIMALS,
        liquidation_threshold=lt / 10_000.0,
        ltv_max=ltv_max / 10_000.0,
        health_factor=float("inf") if total_debt == 0 else hf / _HF_WAD,
        emode=emode,
    )


def _token_balances(meta: _TokenMeta, atoken: int, debt: int, wallet: int) -> AaveTokenBalances:
    return AaveTokenBalances(
        atoken_balance=atoken / 10**meta.atoken_decimals,
        variable_debt_balance=debt / 10**meta.var_debt_decimals,
        # Aave mints the aToken one-for-one with the underlying, so its
        # decimals are the underlying's.
        wallet_balance=wallet / 10**meta.atoken_decimals,
    )


def _reserve_rates(reserve_data: tuple[Any, ...]) -> AaveReserveRates:
    # Positions 2 and 4 per the ABI: currentLiquidityRate, currentVariableBorrowRate.
    liquidity_rate_ray: int = reserve_data[2]
    variable_borrow_rate_ray: int = reserve_data[4]
    return AaveReserveRates(
        liquidity_apr=liquidity_rate_ray / _RAY,
        variable_borrow_apr=variable_borrow_rate_ray / _RAY,
    )


class AaveReader:
    """Read-only Aave v3 client. Bound to one user address."""

    def __init__(
        self,
        web3: AsyncWeb3,  # type: ignore[type-arg]
        pool_address: str,
        user_address: str,
        *,
        multicall_address: str | None = None,
    ) -> None:
        self._w3 = web3
        self._user: ChecksumAddress = AsyncWeb3.to_checksum_address(user_address)
        self._pool: AsyncContract = web3.eth.contract(
            address=AsyncWeb3.to_checksum_address(pool_address),
            abi=_POOL_ABI,
        )
        self._multicall: ChecksumAddress | None = (
            AsyncWeb3.to_checksum_address(multicall_address) if multicall_address else None
        )
        # Cache for per-asset immutable metadata (aToken/varDebt addresses and
        # decimals). These don't change over the lifetime of the market — Aave
        # would need a governance upgrade to alter them. Caching cuts 4 RPC
        # calls per snapshot per asset after the first fetch.
        self._token_meta: dict[str, _TokenMeta] = {}
        # The oracle address is resolved once from the pool's AddressesProvider
        # rather than configured: Aave governance can swap the oracle, and a
        # hardcoded address would keep pricing the collateral with the old one.
        self._oracle: AsyncContract | None = None
        self._oracle_unit: int | None = None

    async def read_snapshot(
        self,
        wsteth_address: str,
        usdc_address: str,
        weth_address: str,
    ) -> AaveSnapshotReads:
        """Every Aave number of a cycle, in one eth_call through Multicall3.

        Eleven separate reads cost eleven requests — plus, before the provider
        cached it, a chain-id lookup for each. At a 5 s cadence that is ~160 M
        compute units a month, five times the free RPC allowance the M1 run had
        already spent 80 % of. Batched, it is one request.

        The batch also fixes something the separate reads never guaranteed:
        all the numbers come from the SAME block. Before, the health factor and
        the balances behind it could straddle a block boundary.

        The immutable metadata (token addresses, decimals, oracle) is fetched
        with single calls on the first cycle and cached, as before.
        """
        multicall = self._multicall
        if multicall is None:
            raise MulticallError("aucune adresse Multicall3 configurée pour le lecteur Aave")
        wsteth_meta, usdc_meta, (oracle, unit) = await asyncio.gather(
            self._get_token_meta(wsteth_address),
            self._get_token_meta(usdc_address),
            self._get_oracle(),
        )
        pool = self._pool.address
        user = self._user
        wsteth = AsyncWeb3.to_checksum_address(wsteth_address)
        usdc = AsyncWeb3.to_checksum_address(usdc_address)
        weth = AsyncWeb3.to_checksum_address(weth_address)
        erc20 = _ERC20_BALANCE_ABI
        calls = [
            _call("getUserAccountData", pool, _POOL_ABI, "getUserAccountData", user),
            _call("getUserEMode", pool, _POOL_ABI, "getUserEMode", user),
            _call("aToken wstETH", wsteth_meta.atoken_address, erc20, "balanceOf", user),
            _call("dette wstETH", wsteth_meta.var_debt_address, erc20, "balanceOf", user),
            _call("solde libre wstETH", wsteth, erc20, "balanceOf", user),
            _call("aToken USDC", usdc_meta.atoken_address, erc20, "balanceOf", user),
            _call("dette USDC", usdc_meta.var_debt_address, erc20, "balanceOf", user),
            _call("solde libre USDC", usdc, erc20, "balanceOf", user),
            _call("getReserveData USDC", pool, _POOL_ABI, "getReserveData", usdc),
            _call("prix oracle wstETH", oracle.address, _ORACLE_ABI, "getAssetPrice", wsteth),
            _call("prix oracle WETH", oracle.address, _ORACLE_ABI, "getAssetPrice", weth),
            _call("solde ETH (gaz)", multicall, _MULTICALL3_ABI, "getEthBalance", user),
        ]
        (
            account_tuple,
            (emode,),
            (wsteth_atoken,),
            (wsteth_debt,),
            (wsteth_wallet,),
            (usdc_atoken,),
            (usdc_debt,),
            (usdc_wallet,),
            usdc_reserve,
            (wsteth_price,),
            (weth_price,),
            (gas_wei,),
        ) = await self._aggregate(multicall, calls)
        return AaveSnapshotReads(
            account=_account_data(account_tuple, emode),
            wsteth=_token_balances(wsteth_meta, wsteth_atoken, wsteth_debt, wsteth_wallet),
            usdc=_token_balances(usdc_meta, usdc_atoken, usdc_debt, usdc_wallet),
            usdc_rates=_reserve_rates(usdc_reserve),
            gas_eth=gas_wei / 1e18,
            oracle=AaveOraclePrices(wsteth_usd=wsteth_price / unit, weth_usd=weth_price / unit),
        )

    async def _aggregate(
        self, multicall: ChecksumAddress, calls: list[_Call]
    ) -> list[tuple[Any, ...]]:
        """Run `calls` through Multicall3.aggregate3 and decode each result.

        `allowFailure` is set on every call so that a revert comes back as a
        flag we can NAME, instead of one opaque revert of the whole batch.
        """
        payload = abi_encode(
            ["(address,bool,bytes)[]"],
            [[(c.target, True, c.calldata) for c in calls]],
        )
        raw = await self._w3.eth.call({"to": multicall, "data": _AGGREGATE3_SELECTOR + payload})
        if not raw:
            # A call to an address without code succeeds and returns nothing.
            raise MulticallError(
                f"Multicall3 muet à {multicall} : aucun contrat à cette adresse sur cette chaîne ?"
            )
        (results,) = abi_decode(["(bool,bytes)[]"], raw)
        if len(results) != len(calls):
            raise MulticallError(
                f"Multicall3 a rendu {len(results)} résultats pour {len(calls)} appels"
            )

        failed = [c.label for c, (ok, _data) in zip(calls, results, strict=True) if not ok]
        if failed:
            raise MulticallError(f"lecture Aave groupée en échec : {', '.join(failed)}")

        decoded: list[tuple[Any, ...]] = []
        for c, (_ok, data) in zip(calls, results, strict=True):
            try:
                decoded.append(tuple(abi_decode(list(c.out_types), data)))
            except Exception as e:
                # Same shape as a missing contract: the target answered, with
                # nothing decodable. A wrong token address in the config lands here.
                raise MulticallError(
                    f"réponse illisible pour {c.label} ({len(data)} octets)"
                ) from e
        return decoded

    async def read_account_data(self) -> AaveAccountData:
        # Parallel: getUserAccountData + getUserEMode.
        (account_tuple, emode) = await asyncio.gather(
            self._pool.functions.getUserAccountData(self._user).call(),
            self._pool.functions.getUserEMode(self._user).call(),
        )
        return _account_data(account_tuple, emode)

    async def _get_token_meta(self, asset: str) -> _TokenMeta:
        """Return cached `_TokenMeta` for `asset`, fetching once on cache miss."""
        key = asset.lower()
        cached = self._token_meta.get(key)
        if cached is not None:
            return cached

        reserve_data = await self._pool.functions.getReserveData(
            AsyncWeb3.to_checksum_address(asset),
        ).call()
        atoken_addr: ChecksumAddress = AsyncWeb3.to_checksum_address(reserve_data[8])
        var_debt_addr: ChecksumAddress = AsyncWeb3.to_checksum_address(reserve_data[10])
        atoken = self._w3.eth.contract(address=atoken_addr, abi=_ERC20_BALANCE_ABI)
        var_debt = self._w3.eth.contract(address=var_debt_addr, abi=_ERC20_BALANCE_ABI)
        atoken_dec, var_debt_dec = await asyncio.gather(
            atoken.functions.decimals().call(),
            var_debt.functions.decimals().call(),
        )
        meta = _TokenMeta(
            atoken_address=atoken_addr,
            var_debt_address=var_debt_addr,
            atoken_decimals=atoken_dec,
            var_debt_decimals=var_debt_dec,
        )
        self._token_meta[key] = meta
        return meta

    async def read_token_balances(self, asset: str) -> AaveTokenBalances:
        """The three balances of one asset: supplied, owed, and free.

        The third one is new, and it is the one whose absence cost the marche à
        blanc two days. `atoken` says what is deposited and `variableDebt` says
        what is owed, but neither says what the wallet can actually spend — so
        the tracer kept depositing 5 USDC per cycle until the wallet was empty,
        then failed 86 times with nothing anywhere to explain it.

        Read here rather than in a separate call because `asyncio.gather` loses
        its element types past six tasks, and because the three numbers belong
        together anyway: they are the same asset seen from three sides.
        """
        meta = await self._get_token_meta(asset)
        atoken = self._w3.eth.contract(address=meta.atoken_address, abi=_ERC20_BALANCE_ABI)
        var_debt = self._w3.eth.contract(address=meta.var_debt_address, abi=_ERC20_BALANCE_ABI)
        underlying = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(asset),
            abi=_ERC20_BALANCE_ABI,
        )
        # Parallel balance reads.
        atoken_bal, vdebt_bal, wallet_bal = await asyncio.gather(
            atoken.functions.balanceOf(self._user).call(),
            var_debt.functions.balanceOf(self._user).call(),
            underlying.functions.balanceOf(self._user).call(),
        )
        return _token_balances(meta, atoken_bal, vdebt_bal, wallet_bal)

    async def read_reserve_rates(self, asset: str) -> AaveReserveRates:
        """Return liquidity + variable-borrow APRs for `asset`.

        Aave stores rates in RAY (1e27) as annualized values, so APR is simply
        `rate / 1e27`. Do NOT re-scale by seconds/year — that is a common bug.
        """
        reserve_data = await self._pool.functions.getReserveData(
            AsyncWeb3.to_checksum_address(asset),
        ).call()
        return _reserve_rates(reserve_data)

    async def _get_oracle(self) -> tuple[AsyncContract, int]:
        """Resolve and cache the price oracle the pool currently points at."""
        if self._oracle is None or self._oracle_unit is None:
            provider_address = await self._pool.functions.ADDRESSES_PROVIDER().call()
            provider = self._w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(provider_address),
                abi=_ADDRESSES_PROVIDER_ABI,
            )
            oracle_address = await provider.functions.getPriceOracle().call()
            oracle = self._w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(oracle_address),
                abi=_ORACLE_ABI,
            )
            self._oracle_unit = await oracle.functions.BASE_CURRENCY_UNIT().call()
            self._oracle = oracle
            log.info(
                "aave_oracle_resolved",
                message="oracle Aave résolu depuis l'AddressesProvider",
                oracle=str(oracle_address),
            )
        return self._oracle, self._oracle_unit

    async def read_oracle_prices(self, wsteth: str, weth: str) -> AaveOraclePrices:
        """Return wstETH and WETH prices as Aave prices them."""
        oracle, unit = await self._get_oracle()
        wsteth_raw, weth_raw = await asyncio.gather(
            oracle.functions.getAssetPrice(AsyncWeb3.to_checksum_address(wsteth)).call(),
            oracle.functions.getAssetPrice(AsyncWeb3.to_checksum_address(weth)).call(),
        )
        return AaveOraclePrices(
            wsteth_usd=wsteth_raw / unit,
            weth_usd=weth_raw / unit,
        )

    async def read_gas_balance_eth(self) -> float:
        wei: int = await self._w3.eth.get_balance(self._user)
        return wei / 1e18
