"""Aave v3 read-only wrapper (M0).

Provides:
- `read_account_data`: HF, LTV, collateral, debt via `Pool.getUserAccountData`.
- `read_atoken_balance` / `read_debt_balance`: raw ERC-20 balances.
- `read_reserve_config`: LT, LTV max, borrow APR — used at boot to validate
  the config guardrails (README section 4).
- `read_emode`: must be 0 in nominal operation (README section 8.1).

No writes. No approvals. No mutations. That is the point of M0.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eth_typing import ChecksumAddress
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


@dataclass(frozen=True, slots=True)
class AaveReserveRates:
    """Interest rates for one reserve (already converted from RAY to APR)."""

    liquidity_apr: float  # supply-side APR
    variable_borrow_apr: float  # variable borrow APR — the one used by the bot


@dataclass(frozen=True, slots=True)
class _TokenMeta:
    """Immutable per-asset metadata cached forever after first fetch."""

    atoken_address: ChecksumAddress
    var_debt_address: ChecksumAddress
    atoken_decimals: int
    var_debt_decimals: int


class AaveReader:
    """Read-only Aave v3 client. Bound to one user address."""

    def __init__(
        self,
        web3: AsyncWeb3,  # type: ignore[type-arg]
        pool_address: str,
        user_address: str,
    ) -> None:
        self._w3 = web3
        self._user: ChecksumAddress = AsyncWeb3.to_checksum_address(user_address)
        self._pool: AsyncContract = web3.eth.contract(
            address=AsyncWeb3.to_checksum_address(pool_address),
            abi=_POOL_ABI,
        )
        # Cache for per-asset immutable metadata (aToken/varDebt addresses and
        # decimals). These don't change over the lifetime of the market — Aave
        # would need a governance upgrade to alter them. Caching cuts 4 RPC
        # calls per snapshot per asset after the first fetch.
        self._token_meta: dict[str, _TokenMeta] = {}

    async def read_account_data(self) -> AaveAccountData:
        # Parallel: getUserAccountData + getUserEMode.
        (account_tuple, emode) = await asyncio.gather(
            self._pool.functions.getUserAccountData(self._user).call(),
            self._pool.functions.getUserEMode(self._user).call(),
        )
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
        """Read aToken and variableDebtToken balances for a given underlying."""
        meta = await self._get_token_meta(asset)
        atoken = self._w3.eth.contract(address=meta.atoken_address, abi=_ERC20_BALANCE_ABI)
        var_debt = self._w3.eth.contract(address=meta.var_debt_address, abi=_ERC20_BALANCE_ABI)
        # Parallel balance reads.
        atoken_bal, vdebt_bal = await asyncio.gather(
            atoken.functions.balanceOf(self._user).call(),
            var_debt.functions.balanceOf(self._user).call(),
        )
        return AaveTokenBalances(
            atoken_balance=atoken_bal / 10**meta.atoken_decimals,
            variable_debt_balance=vdebt_bal / 10**meta.var_debt_decimals,
        )

    async def read_reserve_rates(self, asset: str) -> AaveReserveRates:
        """Return liquidity + variable-borrow APRs for `asset`.

        Aave stores rates in RAY (1e27) as annualized values, so APR is simply
        `rate / 1e27`. Do NOT re-scale by seconds/year — that is a common bug.
        """
        reserve_data = await self._pool.functions.getReserveData(
            AsyncWeb3.to_checksum_address(asset),
        ).call()
        # Positions 2 and 4 per the ABI: currentLiquidityRate, currentVariableBorrowRate.
        liquidity_rate_ray: int = reserve_data[2]
        variable_borrow_rate_ray: int = reserve_data[4]
        return AaveReserveRates(
            liquidity_apr=liquidity_rate_ray / _RAY,
            variable_borrow_apr=variable_borrow_rate_ray / _RAY,
        )

    async def read_gas_balance_eth(self) -> float:
        wei: int = await self._w3.eth.get_balance(self._user)
        return wei / 1e18
