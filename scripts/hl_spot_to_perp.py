"""One-shot: move USDC between Hyperliquid's spot and perp sub-accounts.

Written after the first live bridge crossing (2026-09-02) left 29.8 USDC
stranded on the spot side. `round_trip` now rebalances on its own, but that
only covers future crossings — an imbalance already created has to be undone
by hand, and the exchange UI offers three destinations that are easy to
confuse (Perps, HyperEVM, withdraw to Arbitrum).

    uv run python scripts/hl_spot_to_perp.py                # dry run, shows the plan
    uv run python scripts/hl_spot_to_perp.py --execute      # moves everything to perp
    uv run python scripts/hl_spot_to_perp.py --amount 20 --execute
    uv run python scripts/hl_spot_to_perp.py --to-spot --amount 5 --execute

Why perp is usually the right direction (see memory/hl_findings.md):
  - perp margin is what a post-only order needs
  - `withdraw_from_bridge` draws on perp
  - only the bridge deposit lands in spot

This is an internal transfer. It never leaves Hyperliquid, costs no fee, and
touches no chain — unlike the "EVM" destination in the UI, which sends funds
to HyperEVM and out of reach of both the orders and the withdrawal.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from delta0.config import load_config
from delta0.settings import load_settings


def _usdc_in_spot(state: Any) -> float:
    if not isinstance(state, dict):
        return 0.0
    for entry in state.get("balances", []):
        if isinstance(entry, dict) and entry.get("coin") == "USDC":
            try:
                return float(entry.get("total", "0"))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _usdc_in_perp(state: Any) -> float:
    if not isinstance(state, dict):
        return 0.0
    margin = state.get("marginSummary")
    if not isinstance(margin, dict):
        return 0.0
    try:
        return float(margin.get("accountValue", "0"))
    except (TypeError, ValueError):
        return 0.0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true", help="envoie réellement le transfert")
    p.add_argument(
        "--amount",
        type=float,
        default=None,
        help="montant en USDC (défaut : tout le solde de la source)",
    )
    p.add_argument(
        "--to-spot",
        action="store_true",
        help="inverse le sens : perp -> spot (défaut : spot -> perp)",
    )
    return p.parse_args()


async def main() -> int:
    args = _parse_args()
    to_perp = not args.to_spot

    settings = load_settings()
    cfg = load_config(Path("config.yaml"))
    addr = settings.bot_master_address

    info = await asyncio.to_thread(Info, cfg.venues.hl_api, True)
    spot_state = await asyncio.to_thread(info.spot_user_state, addr)
    perp_state = await asyncio.to_thread(info.user_state, addr)

    spot = _usdc_in_spot(spot_state)
    perp = _usdc_in_perp(perp_state)
    source, source_name = (spot, "spot") if to_perp else (perp, "perp")
    dest_name = "perp" if to_perp else "spot"

    print(f"compte     : {addr}")
    print(f"spot       : {spot:.4f} USDC")
    print(f"perp       : {perp:.4f} USDC")
    print(f"sens       : {source_name} -> {dest_name}")
    print()

    amount = args.amount if args.amount is not None else source
    if amount <= 0:
        print(f"Rien a transferer — le compte {source_name} est vide.")
        return 0
    if amount > source:
        print(f"REFUS: {amount} USDC demandes, {source:.4f} disponibles sur le {source_name}.")
        return 1

    print(f"transfert  : {amount:.4f} USDC  {source_name} -> {dest_name}")

    if not args.execute:
        print("\nMODE SIMULATION — relancer avec --execute pour envoyer.")
        return 0

    pkey = settings.bot_master_private_key.get_secret_value()
    if not pkey or pkey.startswith("REPLACE"):
        print("REFUS: BOT_MASTER_PRIVATE_KEY manquante dans .env")
        return 3

    exchange = await asyncio.to_thread(
        Exchange,
        Account.from_key(pkey),
        cfg.venues.hl_api,
    )
    result = await asyncio.to_thread(exchange.usd_class_transfer, amount, to_perp)
    print(f"reponse HL : {result}")

    detail = str(result.get("response", result)) if isinstance(result, dict) else str(result)
    if isinstance(result, dict) and result.get("status") == "ok":
        pass
    elif "unified account" in detail.lower():
        print(
            "\nCompte UNIFIE : spot et perp partagent un solde unique, "
            "le transfert n'a pas lieu d'etre.\n"
            "Le solde spot sert deja de marge aux positions perp — il n'y a "
            "rien a deplacer, les ordres\net le retrait puisent dedans "
            "directement. Voir memory/hl_findings.md §1.",
        )
    else:
        print("\nATTENTION: reponse inattendue — verifier les soldes ci-dessous.")

    after_spot = _usdc_in_spot(await asyncio.to_thread(info.spot_user_state, addr))
    after_perp = _usdc_in_perp(await asyncio.to_thread(info.user_state, addr))
    print()
    print(f"spot final : {after_spot:.4f} USDC")
    print(f"perp final : {after_perp:.4f} USDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
