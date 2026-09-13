"""Hyperliquid read-only wrapper (M0).

Uses the Info endpoint of the official SDK. No orders, no withdrawals.
Provides:
- `read_mark_price(coin)`: current mark price for a perp.
- `read_position(coin)`: signed position size, entry, isolated margin.
- `read_funding_avg_30d(coin)`: annualized 30-day mean hourly funding.
- `read_maintenance_margin(coin)`: for coherence check vs config at boot.

Threading model: the official SDK is sync — we offload to a thread here.
The rest of the bot is asyncio; wrappers hide the sync/async gap.

Every read turns an answer it cannot use into `HLReadError`, a venue error the
loop survives, rather than letting a KeyError or TypeError escape: since
chantier 6.4 those stop the process, and a malformed venue answer is an outage,
not a bug in the bot.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

from delta0.hl_client import HLReadError, make_info
from delta0.logging import get_logger

log = get_logger(__name__)

_HOURS_PER_YEAR = 24 * 365

# What a malformed answer raises while it is being picked apart.
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, IndexError, AttributeError)


@dataclass(frozen=True, slots=True)
class HLPosition:
    coin: str
    size_signed: float  # negative = short
    entry_price: float
    isolated_margin_usd: float
    leverage: int


@dataclass(frozen=True, slots=True)
class HLMarketMeta:
    coin: str
    mark_price: float
    maintenance_margin_ratio: float  # e.g. 0.02


class HyperliquidReader:
    """Read-only Hyperliquid client. Bound to one user address."""

    def __init__(self, api_url: str, user_address: str) -> None:
        self._user = user_address
        self._info = make_info(api_url, websocket=False)

    async def _run(self, what: str, fn: Any, *args: Any) -> Any:
        result = await asyncio.to_thread(fn, *args)
        # The SDK returns this instead of raising when the body is not JSON.
        if isinstance(result, dict) and set(result) == {"error"}:
            raise HLReadError(f"lecture Hyperliquid {what} en échec : {result['error']}")
        return result

    async def read_mark_price(self, coin: str) -> float:
        mids = await self._run("prix mark", self._info.all_mids)
        try:
            return float(mids[coin])
        except _SHAPE_ERRORS as e:
            raise HLReadError(f"prix mark de {coin!r} absent ou illisible") from e

    async def read_market_meta(self, coin: str) -> HLMarketMeta:
        meta = await self._run("méta du marché", self._info.meta)
        try:
            universe: list[dict[str, Any]] = meta.get("universe", [])
            entry = next((e for e in universe if e.get("name") == coin), None)
            if entry is None:
                raise HLReadError(f"{coin!r} absent de l'univers Hyperliquid")
            # maxLeverage e.g. 50 -> maintenance ratio is HL-specific.
            # Hyperliquid publishes maintenance leverage; we approximate mm = 1/(2 * maxLev)
            # per public docs and re-verify against `clearinghouseState` at boot.
            max_lev = float(entry.get("maxLeverage", 0)) or 1.0
        except _SHAPE_ERRORS as e:
            raise HLReadError(f"méta Hyperliquid illisible pour {coin!r}") from e
        mm = 1.0 / (2.0 * max_lev)
        mark = await self.read_mark_price(coin)
        return HLMarketMeta(coin=coin, mark_price=mark, maintenance_margin_ratio=mm)

    async def read_position(self, coin: str) -> HLPosition | None:
        state = await self._run("état du compte", self._info.user_state, self._user)
        try:
            asset_positions: list[dict[str, Any]] = state.get("assetPositions", [])
            for ap in asset_positions:
                pos: dict[str, Any] = ap.get("position", {})
                if pos.get("coin") != coin:
                    continue
                leverage_obj: dict[str, Any] = pos.get("leverage", {})
                lev_type = leverage_obj.get("type")
                if lev_type != "isolated":
                    log.warning(
                        "hl_position_not_isolated",
                        message="position HL en marge cross — attendu: isolée",
                        coin=coin,
                        leverage_type=lev_type,
                    )
                return HLPosition(
                    coin=coin,
                    size_signed=float(pos.get("szi", 0.0)),
                    entry_price=float(pos.get("entryPx", 0.0)),
                    isolated_margin_usd=float(pos.get("marginUsed", 0.0)),
                    leverage=int(leverage_obj.get("value", 0)),
                )
        except _SHAPE_ERRORS as e:
            raise HLReadError(f"position Hyperliquid illisible pour {coin!r}") from e
        return None

    async def read_funding_avg_30d(self, coin: str) -> float:
        """Annualized 30-day average hourly funding for `coin`.

        Hyperliquid's `funding_history` returns hourly rates; we take the mean
        over the last 720 samples and annualize.
        """
        # 30 days back in ms.
        start_time_ms = self._now_ms() - 30 * 24 * 3600 * 1000
        history = await self._run(
            "historique de funding", self._info.funding_history, coin, start_time_ms
        )
        try:
            if not history:
                return 0.0
            rates: list[float] = [float(h["fundingRate"]) for h in history]
        except _SHAPE_ERRORS as e:
            raise HLReadError(f"historique de funding illisible pour {coin!r}") from e
        mean_hourly = sum(rates) / len(rates)
        return mean_hourly * _HOURS_PER_YEAR

    async def read_last_hour_funding(self, coin: str) -> float:
        start_time_ms = self._now_ms() - 2 * 3600 * 1000
        history = await self._run(
            "funding de la dernière heure", self._info.funding_history, coin, start_time_ms
        )
        try:
            if not history:
                return 0.0
            return float(history[-1]["fundingRate"])
        except _SHAPE_ERRORS as e:
            raise HLReadError(f"funding de la dernière heure illisible pour {coin!r}") from e

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    async def read_user_summary(self) -> dict[str, Any]:
        """Full clearinghouse state — useful for `status` command."""
        return cast(
            dict[str, Any], await self._run("état du compte", self._info.user_state, self._user)
        )

    async def read_free_usdc(self) -> float:
        """USDC available on the account but not committed as isolated margin.

        Read from the SPOT state, not from `withdrawable`. On a unified account
        — which ours is — `withdrawable` reports 0 the moment a position exists,
        even while a withdrawal of that size succeeds; sizing anything on it
        would conclude there is nothing to draw on. Measured 2026-09-09, see
        memory/hl_findings.md §14 and §16.

        This is what the fast up-flank defence spends: adding isolated margin
        from here is one local request, no bridge.
        """
        state = await self._run("solde spot", self._info.spot_user_state, self._user)
        if not isinstance(state, dict):
            return 0.0
        balances = state.get("balances")
        if not isinstance(balances, list):
            return 0.0
        for entry in balances:
            if isinstance(entry, dict) and entry.get("coin") == "USDC":
                try:
                    return float(entry.get("total", "0"))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0
