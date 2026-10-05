"""Mesure D2 : ce que Hyperliquid rapporte d'une position isolée sur NOTRE compte.

Seconde revue finance du 2026-10-05, désaccord D2 : la sémantique de
`marginUsed` (PnL latent compris ou non) et la maintenance margin à 2 % ont été
reproduites à 0,0000 % sur dix positions de comptes tiers, jamais sur le nôtre.
Tout le flanc haut en dépend (P2, P5, I3), et le résidu de F1 aussi : sur notre
compte unifié, `hold` vaut-il bien la marge engagée ?

    uv run python scripts/hl_probe_position.py              # lecture seule : état du compte
    uv run python scripts/hl_probe_position.py --execute    # ouvre, mesure, referme

Avec `--execute` : short ETH isolé 10x de 0,005 ETH (≈ 13 $ de notionnel, au-dessus
du minimum de 10 $), relevé de `marginUsed`, `hold`, `total`, du PnL latent et de
`liquidationPx`, puis fermeture en reduce-only. Coût attendu : deux frais taker
de 4,5 bps sur 13 $, soit ≈ 0,01 $. Signé par la clé maître du `.env` (l'agent
n'est câblé qu'au chantier 5.3). Aucune alerte n'est émise.

Ce que le script vérifie, et rend dans `data/hl_probe-<date>.json` :
  1. le `liquidationPx` publié contre les deux lectures de `marginUsed` :
     avec PnL, P = (marginUsed + |szi|·mark) / (|szi|·(1 + MM)) ;
     sans PnL, la même formule avec marginUsed + PnL latent ;
  2. `hold` contre `marginUsed` sur le solde spot USDC ;
  3. `total - hold`, la réserve libre que le bot lit depuis F1.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from delta0.config import load_config  # noqa: E402
from delta0.hl_client import make_exchange, make_info  # noqa: E402
from delta0.settings import load_settings  # noqa: E402

COIN = "ETH"
SIZE_ETH = 0.005
LEVERAGE = 10
SLIPPAGE = 0.01
MAINTENANCE = 0.02  # 1 / (2 x 25), relue ci-dessous sur la méta de la place
SETTLE_S = 2.0


def spot_usdc(info: Any, user: str) -> dict[str, float]:
    state = info.spot_user_state(user)
    for entry in state.get("balances", []):
        if entry.get("coin") == "USDC":
            return {"total": float(entry["total"]), "hold": float(entry["hold"])}
    return {"total": 0.0, "hold": 0.0}


def eth_position(info: Any, user: str) -> dict[str, Any] | None:
    for item in info.user_state(user).get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == COIN:
            return dict(position)
    return None


def max_leverage(info: Any) -> int:
    for entry in info.meta().get("universe", []):
        if entry.get("name") == COIN:
            return int(entry["maxLeverage"])
    raise SystemExit(f"{COIN} absent de l'univers Hyperliquid")


def implied_liquidation(margin: float, size: float, mark: float, mm: float) -> float:
    """Prix où l'équité isolée d'un short tombe à MM x notionnel."""
    return (margin + size * mark) / (size * (1.0 + mm))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="ouvre puis referme la position")
    args = parser.parse_args()

    settings = load_settings()
    config = load_config(REPO / "config.yaml")
    user = settings.bot_master_address
    info = make_info(config.venues.hl_api, websocket=False)

    before = spot_usdc(info, user)
    existing = eth_position(info, user)
    mm = 1.0 / (2.0 * max_leverage(info))
    print(f"compte {user}  spot USDC total {before['total']:.4f}  hold {before['hold']:.4f}")
    print(f"maintenance margin lue : {mm:.4f}")
    if existing is not None and float(existing.get("szi", 0.0)) != 0.0:
        print(f"position {COIN} déjà ouverte : {existing} — refus, rien n'est fait")
        return 1
    if not args.execute:
        print("lecture seule ; --execute pour ouvrir, mesurer et refermer")
        return 0

    key = settings.bot_master_private_key.get_secret_value()
    if not key:
        raise SystemExit("BOT_MASTER_PRIVATE_KEY vide dans .env")
    exchange = make_exchange(key, config.venues.hl_api)

    record: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "user": user, "before": before}
    record["leverage"] = exchange.update_leverage(LEVERAGE, COIN, is_cross=False)
    record["open"] = exchange.market_open(COIN, False, SIZE_ETH, None, SLIPPAGE)
    print("ouverture :", json.dumps(record["open"])[:300])
    try:
        time.sleep(SETTLE_S)
        position = eth_position(info, user)
        during = spot_usdc(info, user)
        mark = float(info.all_mids()[COIN])
        record.update(position=position, during=during, mid=mark)
        if position is None:
            print("aucune position relevée : l'ouverture a été refusée, voir ci-dessus")
            return 1
        size = abs(float(position["szi"]))
        margin_used = float(position["marginUsed"])
        upnl = float(position["unrealizedPnl"])
        published = float(position["liquidationPx"])
        with_pnl = implied_liquidation(margin_used, size, mark, mm)
        without_pnl = implied_liquidation(margin_used + upnl, size, mark, mm)
        record["checks"] = {
            "liquidation_published": published,
            "liquidation_if_marginUsed_includes_pnl": with_pnl,
            "liquidation_if_marginUsed_excludes_pnl": without_pnl,
            "gap_with_pnl_pct": 100 * (with_pnl / published - 1),
            "gap_without_pnl_pct": 100 * (without_pnl / published - 1),
            "hold_minus_marginUsed": during["hold"] - margin_used,
            "free_reserve_total_minus_hold": during["total"] - during["hold"],
        }
        for name, value in record["checks"].items():
            print(f"  {name:<42} {value:.6f}")
    finally:
        record["close"] = exchange.market_close(COIN, None, None, SLIPPAGE)
        print("fermeture :", json.dumps(record["close"])[:300])
        time.sleep(SETTLE_S)
        record["after"] = spot_usdc(info, user)
        record["position_after"] = eth_position(info, user)
        out = REPO / "data" / f"hl_probe-{date.today().isoformat()}.json"
        out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        print(
            f"après : total {record['after']['total']:.4f}  hold {record['after']['hold']:.4f}  "
            f"coût {before['total'] - record['after']['total']:.4f} USDC -> {out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
