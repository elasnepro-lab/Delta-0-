"""Read-only dump of the Aave v3 Arbitrum parameters the strategy depends on.

Answers "are the configured LTV thresholds below the real liquidation
threshold?" — the question that gates every emergency priority. Sends no
transaction and touches no database, so it is safe to run while the tracer is
live.

    uv run python scripts/read_aave_params.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from web3 import Web3
from web3.contract import Contract

REPO = Path(__file__).resolve().parents[1]

# A threshold sitting less than this below the liquidation threshold leaves no
# room for the priority to act before Aave does.
MIN_MARGIN_TO_LT = 0.01

PUBLIC_RPCS = (
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum-one.publicnode.com",
)
WETH = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
BPS = 10_000

DATA_PROVIDER_ABI: list[dict[str, Any]] = [
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
    {
        "name": "getReserveCaps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "borrowCap", "type": "uint256"},
            {"name": "supplyCap", "type": "uint256"},
        ],
    },
    {
        "name": "getPaused",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "isPaused", "type": "bool"}],
    },
    {
        "name": "getATokenTotalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getTotalDebt",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getLiquidationProtocolFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

POOL_ABI: list[dict[str, Any]] = [
    {
        "name": "ADDRESSES_PROVIDER",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "getEModeCategoryData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "id", "type": "uint8"}],
        "outputs": [
            {
                "components": [
                    {"name": "ltv", "type": "uint16"},
                    {"name": "liquidationThreshold", "type": "uint16"},
                    {"name": "liquidationBonus", "type": "uint16"},
                    {"name": "priceSource", "type": "address"},
                    {"name": "label", "type": "string"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
    },
]

ADDRESSES_PROVIDER_ABI: list[dict[str, Any]] = [
    {
        "name": "getPriceOracle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]

ORACLE_ABI: list[dict[str, Any]] = [
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
    {
        "name": "getSourceOfAsset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def rpc_urls() -> list[str]:
    """Endpoints from .env first, then the public ones as a fallback."""
    urls: list[str] = []
    env = REPO / ".env"
    if env.exists():
        prefixes = ("ARBITRUM_RPC_PRIMARY=", "ARBITRUM_RPC_FALLBACK=")
        for raw in env.read_text(encoding="utf-8").splitlines():
            entry = raw.strip()
            if entry.startswith(prefixes):
                value = entry.split("=", 1)[1]
                urls += [u.strip() for u in value.split(",") if u.strip()]
    urls += list(PUBLIC_RPCS)
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def redact(url: str) -> str:
    """Hide the API key that lives after /v2/ in provider URLs."""
    return f"{url.split('/v2/', maxsplit=1)[0]}/v2/***" if "/v2/" in url else url


def connect() -> Web3:
    for url in rpc_urls():
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                print(f"RPC   : {redact(url)}")
                print(f"bloc  : {w3.eth.block_number}")
                return w3
        except Exception as exc:
            print(f"  ({redact(url)} injoignable : {type(exc).__name__})")
    raise SystemExit("aucun endpoint RPC joignable")


def call(contract: Contract, fn: str, *args: Any) -> Any:
    """Call a view, returning None when the function is absent or reverts."""
    try:
        return getattr(contract.functions, fn)(*args).call()
    except Exception as exc:
        print(f"    ({fn} indisponible : {type(exc).__name__})")
        return None


def show_reserve(dp: Contract, name: str, asset: str) -> float | None:
    """Print one reserve's configuration. Returns its liquidation threshold."""
    cfg = call(dp, "getReserveConfigurationData", asset)
    if cfg is None:
        return None
    decimals, ltv, lt, bonus, reserve_factor = cfg[0], cfg[1], cfg[2], cfg[3], cfg[4]
    collateral, borrowable, _, active, frozen = cfg[5], cfg[6], cfg[7], cfg[8], cfg[9]

    caps = call(dp, "getReserveCaps", asset)
    paused = call(dp, "getPaused", asset)
    fee = call(dp, "getLiquidationProtocolFee", asset)
    supplied = call(dp, "getATokenTotalSupply", asset)
    borrowed = call(dp, "getTotalDebt", asset)

    print(f"\n=== {name} ({asset}) ===")
    print(f"  decimales                : {decimals}")
    print(f"  LTV max                  : {ltv / BPS:.4f}")
    print(f"  liquidation threshold LT : {lt / BPS:.4f}")
    print(f"  bonus de liquidation     : {bonus / BPS:.4f}  -> penalite {(bonus - BPS) / BPS:.4f}")
    if fee is not None:
        print(f"  frais de protocole       : {fee / BPS:.4f}")
    print(f"  reserve factor           : {reserve_factor / BPS:.4f}")
    print(f"  collateral / empruntable : {collateral} / {borrowable}")
    print(f"  actif / gele / en pause  : {active} / {frozen} / {paused}")

    if caps is not None:
        borrow_cap, supply_cap = caps
        unit = 10**decimals
        if supply_cap:
            used = (supplied or 0) / unit
            print(f"  supply cap               : {supply_cap:,} (depose {used:,.0f})")
            print(f"     utilisation           : {100 * used / supply_cap:.1f} %")
            print(f"     marge disponible      : {supply_cap - used:,.0f}")
        if borrow_cap:
            used = (borrowed or 0) / unit
            print(f"  borrow cap               : {borrow_cap:,} (emprunte {used:,.0f})")
            print(f"     utilisation           : {100 * used / borrow_cap:.1f} %")
    return lt / BPS


def show_emode(pool: Contract) -> None:
    print("\n=== categories e-mode ===")
    for category_id in (1, 2, 3, 4):
        data = call(pool, "getEModeCategoryData", category_id)
        if data and data[0]:
            print(
                f"  {category_id} · {data[4]:<38} LTV {data[0] / BPS:.4f}  LT {data[1] / BPS:.4f}"
            )
    print("  (une dette USDC contre du wstETH n'est eligible a aucune : e-mode 0)")


def show_oracle(w3: Web3, pool: Contract, wsteth: str, usdc: str) -> float | None:
    """Print oracle prices and the wstETH/ETH ratio. Returns that ratio."""
    print("\n=== oracle Aave ===")
    provider_address = call(pool, "ADDRESSES_PROVIDER")
    if provider_address is None:
        return None
    provider = w3.eth.contract(address=provider_address, abi=ADDRESSES_PROVIDER_ABI)
    oracle_address = call(provider, "getPriceOracle")
    if oracle_address is None:
        return None
    oracle = w3.eth.contract(address=oracle_address, abi=ORACLE_ABI)
    unit = call(oracle, "BASE_CURRENCY_UNIT") or 10**8

    p_wsteth = call(oracle, "getAssetPrice", wsteth)
    p_weth = call(oracle, "getAssetPrice", WETH)
    p_usdc = call(oracle, "getAssetPrice", usdc)
    if p_wsteth is None or p_weth is None:
        return None

    print(f"  AaveOracle               : {oracle_address}")
    print(f"  prix wstETH              : {p_wsteth / unit:,.2f}")
    print(f"  prix WETH                : {p_weth / unit:,.2f}")
    if p_usdc is not None:
        print(f"  prix USDC                : {p_usdc / unit:.6f}")
    print(f"  source wstETH            : {call(oracle, 'getSourceOfAsset', wsteth)}")

    ratio = p_wsteth / p_weth
    print(f"  ratio wstETH/ETH         : {ratio:.6f}")
    print("  (wstETH.stEthPerToken() n'existe pas sur Arbitrum : jeton ponte.")
    print("   Le ratio vient donc de l'oracle, ce qui le rend coherent avec le HF.)")
    return ratio


def show_verdict(lt: float, cfg: dict[str, Any]) -> None:
    target = cfg["target_ltv"]
    emergency = cfg["emergency"]
    # Since chantier 1.2 the config holds margins below the liquidation
    # threshold, not absolute LTVs: derived here the way decision.derive_bands
    # does. The script still read the removed `ltv_pump` keys and died on a
    # KeyError right after printing the reserves (audit dev 2026-09-16, 6.3).
    thresholds = [
        ("ltv_pump", lt - emergency["ltv_margin_pump"]),
        ("ltv_cushion", lt - emergency["ltv_margin_cushion"]),
        ("ltv_deleverage", lt - emergency["ltv_margin_deleverage"]),
    ]

    print("\n=== verdict sur les seuils configures ===")
    print(f"  LT reel = {lt:.4f}   target_ltv = {target}")
    for name, value in thresholds:
        margin = lt - value
        if margin < 0:
            verdict = "AU-DELA DE LA LIQUIDATION"
        elif margin < MIN_MARGIN_TO_LT:
            verdict = "MARGE INSUFFISANTE"
        else:
            verdict = "ok"
        print(f"    {name:<16} {value:.4f}   marge au LT {margin:+.4f}   {verdict}")

    print(f"\n  bande basse reelle : -{100 * (1 - target / lt):.2f} % depuis LTV {target}")
    for name, value in thresholds:
        print(f"    {name:<16} atteint a -{100 * (1 - target / value):.2f} %")


def main() -> None:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    venues = cfg["venues"]
    wsteth = Web3.to_checksum_address(venues["wsteth_address"])
    usdc = Web3.to_checksum_address(venues["usdc_address"])

    w3 = connect()
    dp = w3.eth.contract(
        address=Web3.to_checksum_address(venues["aave_data_provider"]),
        abi=DATA_PROVIDER_ABI,
    )
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(venues["aave_pool"]),
        abi=POOL_ABI,
    )

    lt = show_reserve(dp, "wstETH (collateral)", wsteth)
    show_reserve(dp, "USDC natif (dette)", usdc)
    show_emode(pool)
    show_oracle(w3, pool, wsteth, usdc)
    if lt is not None:
        show_verdict(lt, cfg)


if __name__ == "__main__":
    main()
