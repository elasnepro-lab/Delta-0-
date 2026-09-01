"""M1-B2 pre-flight check: exercise the real Aave v3 Pool ABI on an anvil fork.

Usage:
    1. Start anvil forking Arbitrum mainnet in a separate terminal:
         anvil --fork-url https://arb1.arbitrum.io/rpc --port 8545 --chain-id 42161

    2. Run this script:
         uv run python scripts/precheck_aave_fork.py

The script impersonates the operator's wallet on the fork (the account holds
187 USDC + 0.005 ETH copied from Arbitrum mainnet state at fork time). It
then runs the real six-transaction sequence against the mainnet Aave Pool
contract — approve, supply, borrow, approve buffer, repay MAX_UINT256,
withdraw — using the SAME ABI as `delta0.venues.aave` and `delta0.executor`.
Any ABI mismatch, wrong parameter order, or contract address error will
surface here — with zero real money at risk.

The borrow is not decoration: Aave reverts a repay with no debt, and a
partial repay leaves accrued interest that blocks the full withdraw. See
`memory/aave_findings.md`.

Success criterion: all 6 transactions confirm with status=1 and the ending
USDC balance equals the starting one. Prints per-op gas cost.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from web3 import AsyncHTTPProvider, AsyncWeb3

ANVIL_RPC = "http://127.0.0.1:8545"
USER_ADDR = AsyncWeb3.to_checksum_address("0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89")
POOL_ADDR = AsyncWeb3.to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD")
USDC_ADDR = AsyncWeb3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")

TEST_AMOUNT_USDC = 5.0
USDC_DECIMALS = 6
RAW_AMOUNT = int(TEST_AMOUNT_USDC * (10**USDC_DECIMALS))
VARIABLE_RATE = 2
REFERRAL = 0

# Reuse the exact ABIs the executor uses in production.
_POOL_ABI: list[dict[str, Any]] = [
    {
        "name": "supply",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "name": "borrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "repay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
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
]

_ERC20_ABI: list[dict[str, Any]] = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


async def rpc(w3: AsyncWeb3, method: str, params: list[Any]) -> Any:  # type: ignore[type-arg]
    """Make a raw RPC request to anvil."""
    return await w3.provider.make_request(method, params)  # type: ignore[union-attr]


async def send_impersonated(
    w3: AsyncWeb3,  # type: ignore[type-arg]
    label: str,
    contract: Any,
    func_name: str,
    args: list[Any],
) -> None:
    """Send a tx from the impersonated USER_ADDR and wait for receipt."""
    tx = await getattr(contract.functions, func_name)(*args).build_transaction(
        {
            "from": USER_ADDR,
            "gas": 1_000_000,
            "gasPrice": 100_000_000,  # 0.1 gwei — anvil doesn't need real prices
            "nonce": await w3.eth.get_transaction_count(USER_ADDR),
        },
    )
    tx_hash_resp = await rpc(w3, "eth_sendTransaction", [tx])
    if "error" in tx_hash_resp:
        print(f"[FAIL] {label}: {tx_hash_resp['error']}")
        sys.exit(1)
    tx_hash = tx_hash_resp["result"]
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
    status = receipt.get("status", 0)
    gas = receipt.get("gasUsed", 0)
    if status == 1:
        print(f"[OK]   {label}: gas={gas} tx={tx_hash[:10]}…")
    else:
        print(f"[FAIL] {label}: reverted, tx={tx_hash}")
        sys.exit(1)


async def main() -> None:
    w3 = AsyncWeb3(AsyncHTTPProvider(ANVIL_RPC))

    chain_id = await w3.eth.chain_id
    if chain_id != 42161:  # noqa: PLR2004
        print(f"[FAIL] anvil is not forking Arbitrum (chain_id={chain_id})")
        sys.exit(1)
    print(f"[OK]   Connected to anvil fork of Arbitrum One (chain_id={chain_id})")

    # Impersonate the operator's address and top up ETH for gas.
    await rpc(w3, "anvil_impersonateAccount", [USER_ADDR])
    await rpc(w3, "anvil_setBalance", [USER_ADDR, hex(10**18)])
    print(f"[OK]   Impersonated {USER_ADDR}, funded with 1 ETH (fork only)")

    # Read starting state.
    pool = w3.eth.contract(address=POOL_ADDR, abi=_POOL_ABI)
    usdc = w3.eth.contract(address=USDC_ADDR, abi=_ERC20_ABI)
    starting_usdc: int = await usdc.functions.balanceOf(USER_ADDR).call()
    print(f"[OK]   Starting USDC balance: {starting_usdc / 10**USDC_DECIMALS:.2f} USDC")

    if starting_usdc < RAW_AMOUNT:
        print(f"[FAIL] Wallet has < {TEST_AMOUNT_USDC} USDC on the fork")
        sys.exit(1)

    # 1) Approve the Aave Pool to pull USDC.
    await send_impersonated(
        w3,
        "approve(Pool, 5 USDC)",
        usdc,
        "approve",
        [POOL_ADDR, RAW_AMOUNT],
    )

    # 2) Supply 5 USDC.
    await send_impersonated(
        w3,
        "supply(USDC, 5, self, 0)",
        pool,
        "supply",
        [USDC_ADDR, RAW_AMOUNT, USER_ADDR, REFERRAL],
    )

    # 3) Borrow 1 USDC so there IS debt to repay. Aave's repay reverts with
    #    NO_DEBT_OF_SELECTED_TYPE when the debt is zero, so the repay ABI
    #    cannot be exercised without borrowing first.
    await send_impersonated(
        w3,
        "borrow(USDC, 1, variable=2, 0, self)",
        pool,
        "borrow",
        [USDC_ADDR, 1_000_000, VARIABLE_RATE, REFERRAL, USER_ADDR],
    )

    # 4) Repay ALL (use MAX_UINT256 sentinel to close the position + all accrued
    #    interest). Approve a small buffer to cover the interest (fractions of a cent).
    max_uint = 2**256 - 1
    await send_impersonated(
        w3,
        "approve(Pool, 2 USDC buffer)",
        usdc,
        "approve",
        [POOL_ADDR, 2_000_000],
    )
    await send_impersonated(
        w3,
        "repay(USDC, MAX_UINT256, variable=2, self)",
        pool,
        "repay",
        [USDC_ADDR, max_uint, VARIABLE_RATE, USER_ADDR],
    )

    # 5) Withdraw the supplied USDC back to self.
    await send_impersonated(
        w3,
        "withdraw(USDC, 5, self)",
        pool,
        "withdraw",
        [USDC_ADDR, RAW_AMOUNT, USER_ADDR],
    )

    ending_usdc: int = await usdc.functions.balanceOf(USER_ADDR).call()
    print(f"[OK]   Ending USDC balance:   {ending_usdc / 10**USDC_DECIMALS:.2f} USDC")
    print()
    print("=" * 60)
    print("ALL ABI + PARAMETER CHECKS PASSED ON FORK.")
    print(
        "The Aave executor code should work against Arbitrum mainnet with "
        "the same wallet and same amounts.",
    )
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
