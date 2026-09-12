"""One-shot: close whatever Aave position the tracer left open.

Written after the first live M1 cycle (2026-09-02) reverted at the `repay`,
leaving 5 USDC of collateral against ~1 USDC of debt. It is deliberately NOT
part of the tracer: unwinding is an operator action, not something a loop
should decide to do on its own.

    uv run python scripts/unwind_aave.py            # dry run, prints the plan
    uv run python scripts/unwind_aave.py --execute  # actually sends

Reads the same `.env` and `config.yaml` as the bot. Refuses to send anything
unless `--execute` is passed, and simulates every step with `eth_call` first —
a step that would revert is reported instead of paid for.

Steps, each skipped when unnecessary:
  1. withdraw(USDC, shortfall)            — only when the wallet cannot fund the repay
  2. approve(Pool, debt + 1 USDC buffer)  — headroom for interest accrued
  3. repay(USDC, MAX_UINT256, mode=2)     — closes debt AND accrued interest
  4. withdraw(USDC, MAX_UINT256)          — rounding-proof, see aave_findings.md

Step 1 exists because the M1 marche à blanc produced the case the original
three-step plan could not handle: the tracer deposited 5 USDC per cycle without
ever taking them back, so the wallet ended poorer than the debt (2,97 against
35,01) and `repay` reverted on `ERC20: transfer amount exceeds balance`. The
collateral is the only source of USDC, so some of it has to come back first.
How much can safely come back is derived from the liquidation threshold read
on-chain, never from a constant.

Gas limits carry the same margin as the executors (`delta0.gas`): the revert
that created this mess is the reason that margin exists.
"""

from __future__ import annotations

import asyncio
import sys
from functools import partial
from pathlib import Path
from typing import Any

from web3 import AsyncHTTPProvider, AsyncWeb3

from delta0.config import load_config
from delta0.gas import with_gas_margin
from delta0.settings import load_settings

MAX_UINT256 = 2**256 - 1
VARIABLE_RATE = 2
USDC_DECIMALS = 6

_POOL_ABI: list[dict[str, Any]] = [
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
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

_DATA_PROVIDER_ABI: list[dict[str, Any]] = [
    {
        "name": "getUserReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
    },
    {
        "name": "getReserveConfigurationData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "decimals", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "liquidationThreshold", "type": "uint256"},
            {"name": "liquidationBonus", "type": "uint256"},
            {"name": "reserveFactor", "type": "uint256"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
            {"name": "borrowingEnabled", "type": "bool"},
            {"name": "stableBorrowRateEnabled", "type": "bool"},
            {"name": "isActive", "type": "bool"},
            {"name": "isFrozen", "type": "bool"},
        ],
    },
]

# Health factor the pre-withdraw must leave standing. The withdraw only has to
# survive the seconds until the repay lands, but a position at HF 1.01 between
# two transactions is not a position we want to own, so ask for a wide margin.
_MIN_HF_AFTER_PREWITHDRAW = 2.0

# Interest keeps accruing between the pre-withdraw and the repay. Same buffer
# the approve already carries, for the same reason.
_REPAY_BUFFER = 10**USDC_DECIMALS


def _usdc(raw: int) -> str:
    return f"{raw / 10**USDC_DECIMALS:.6f}"


def plan_prewithdraw(
    *,
    wallet: int,
    debt: int,
    collateral: int,
    lt_bps: int,
) -> tuple[int, str | None]:
    """How much collateral must come back before the repay can be funded.

    Returns `(amount, refusal)`. `amount` is 0 when the wallet already covers
    the debt plus its buffer. `refusal` is a printable reason when no safe
    amount exists, in which case the caller must not send anything: taking the
    position below the liquidation threshold to close it would be worse than
    leaving it open.
    """
    needed = debt + _REPAY_BUFFER
    if wallet >= needed:
        return 0, None

    shortfall = needed - wallet
    if collateral == 0:
        return 0, (
            f"il manque {_usdc(shortfall)} USDC pour rembourser et il n'y a "
            f"aucun collateral a retirer"
        )
    if lt_bps == 0:
        return 0, (
            "le seuil de liquidation lu on-chain vaut 0 : impossible de borner "
            "un retrait sur, refus"
        )

    # collateral_after * LT >= debt * HF_target, LT en points de base.
    required_after = -(-debt * int(_MIN_HF_AFTER_PREWITHDRAW * 10_000) // lt_bps)
    withdrawable = collateral - required_after
    if shortfall > withdrawable:
        return 0, (
            f"il manque {_usdc(shortfall)} USDC pour rembourser, et seuls "
            f"{_usdc(max(0, withdrawable))} sont retirables en gardant "
            f"HF >= {_MIN_HF_AFTER_PREWITHDRAW:g} (LT {lt_bps / 100:.2f} %)"
        )
    return shortfall, None


async def _send(
    w3: AsyncWeb3,  # type: ignore[type-arg]
    label: str,
    call: Any,
    *,
    sender: str,
    pkey: str,
    chain_id: int,
    execute: bool,
) -> bool:
    """Simulate, then send when `execute`. Returns False if the step reverts."""
    try:
        await call.call({"from": sender})
    except Exception as e:
        if not execute:
            # In dry run nothing is applied, so every step after the first is
            # simulated against a chain state where its predecessor never
            # happened. `withdraw` reverting here only says "the repay has not
            # run yet" — it is not a verdict on the plan. Keep going.
            print(
                f"  [ATTENDU] {label} : revert en simulation "
                f"(l'etape precedente n'est pas appliquee) — {type(e).__name__}",
            )
            return True
        print(f"  [REVERT] {label} -> {type(e).__name__}: {e}")
        return False

    if not execute:
        print(f"  [SIMULE] {label} : OK (relancer avec --execute pour envoyer)")
        return True

    tx = await call.build_transaction(
        {
            "from": sender,
            "nonce": await w3.eth.get_transaction_count(sender),
            "chainId": chain_id,
        },
    )
    tx["gas"] = with_gas_margin(int(tx["gas"]))
    signed = w3.eth.account.sign_transaction(tx, private_key=pkey)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
    ok = int(receipt.get("status", 0)) == 1
    mark = "OK" if ok else "REVERTED"
    print(f"  [{mark}] {label}  gas={receipt['gasUsed']:,}  tx={tx_hash.hex()}")
    return ok


async def main() -> int:
    execute = "--execute" in sys.argv

    settings = load_settings()
    cfg = load_config(Path("config.yaml"))
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.arbitrum_rpc_primary))

    user = AsyncWeb3.to_checksum_address(settings.bot_master_address)
    pool_addr = AsyncWeb3.to_checksum_address(cfg.venues.aave_pool)
    usdc_addr = AsyncWeb3.to_checksum_address(cfg.venues.usdc_address)

    pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    usdc = w3.eth.contract(address=usdc_addr, abi=_ERC20_ABI)
    provider = w3.eth.contract(
        address=AsyncWeb3.to_checksum_address(cfg.venues.aave_data_provider),
        abi=_DATA_PROVIDER_ABI,
    )

    data = await provider.functions.getUserReserveData(usdc_addr, user).call()
    collateral, _stable_debt, debt = data[0], data[1], data[2]
    wallet = await usdc.functions.balanceOf(user).call()
    allowance = await usdc.functions.allowance(user, pool_addr).call()
    reserve = await provider.functions.getReserveConfigurationData(usdc_addr).call()
    lt_bps = int(reserve[2])

    print(f"wallet     : {user}")
    print(f"USDC libre : {_usdc(wallet)}")
    print(f"collateral : {_usdc(collateral)}")
    print(f"dette      : {_usdc(debt)}")
    print(f"allowance  : {_usdc(allowance)}")
    print(f"LT on-chain: {lt_bps / 100:.2f} %")
    print()

    if collateral == 0 and debt == 0:
        print("Rien a deboucler — position deja fermee.")
        return 0

    prewithdraw, refusal = plan_prewithdraw(
        wallet=wallet,
        debt=debt,
        collateral=collateral,
        lt_bps=lt_bps,
    )
    if refusal is not None:
        print(f"REFUS: {refusal}")
        return 4
    if prewithdraw > 0:
        print(
            f"Le portefeuille ne finance pas le repay : retrait prealable de "
            f"{_usdc(prewithdraw)} USDC de collateral.\n",
        )

    if not execute:
        print("MODE SIMULATION — aucune transaction ne sera envoyee.\n")

    pkey = settings.bot_master_private_key.get_secret_value()
    if execute and (not pkey or pkey.startswith("REPLACE")):
        print("REFUS: BOT_MASTER_PRIVATE_KEY manquante dans .env")
        return 3

    chain_id = await w3.eth.chain_id
    step = partial(_send, w3, sender=user, pkey=pkey, chain_id=chain_id, execute=execute)

    if not await _unwind(
        step,
        pool=pool,
        usdc=usdc,
        pool_addr=pool_addr,
        usdc_addr=usdc_addr,
        user=user,
        debt=debt,
        collateral=collateral,
        allowance=allowance,
        prewithdraw=prewithdraw,
    ):
        return 1

    if not execute:
        return 0
    return await _report_final(usdc, provider, usdc_addr, user)


async def _unwind(
    step: Any,
    *,
    pool: Any,
    usdc: Any,
    pool_addr: str,
    usdc_addr: str,
    user: str,
    debt: int,
    collateral: int,
    allowance: int,
    prewithdraw: int = 0,
) -> bool:
    """Run withdraw -> approve -> repay -> withdraw, skipping the unnecessary."""
    if prewithdraw > 0 and not await step(
        f"withdraw(USDC, {_usdc(prewithdraw)}) — financer le repay",
        pool.functions.withdraw(usdc_addr, prewithdraw, user),
    ):
        return False

    if debt > 0:
        headroom = debt + 10**USDC_DECIMALS  # dette + 1 USDC pour les interets
        if allowance >= headroom:
            print(f"  [SAUTE]  approve — allowance {_usdc(allowance)} deja suffisante")
        elif not await step(
            f"approve(Pool, {_usdc(headroom)})",
            usdc.functions.approve(pool_addr, headroom),
        ):
            return False

        if not await step(
            "repay(USDC, MAX_UINT256, mode=2)",
            pool.functions.repay(usdc_addr, MAX_UINT256, VARIABLE_RATE, user),
        ):
            return False

    return not (
        collateral > 0
        and not await step(
            "withdraw(USDC, MAX_UINT256)",
            pool.functions.withdraw(usdc_addr, MAX_UINT256, user),
        )
    )


async def _report_final(
    usdc: Any,
    provider: Any,
    usdc_addr: str,
    user: str,
) -> int:
    """Re-read the position after sending and say plainly whether it is closed."""
    final = await usdc.functions.balanceOf(user).call()
    after = await provider.functions.getUserReserveData(usdc_addr, user).call()
    print()
    print(f"USDC final : {_usdc(final)}")
    print(f"collateral : {_usdc(after[0])}   dette : {_usdc(after[2])}")
    if after[0] == 0 and after[2] == 0:
        print("\nPosition fermee.")
        return 0
    print("\nATTENTION: position encore ouverte — relancer.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
