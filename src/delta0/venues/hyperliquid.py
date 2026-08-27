"""Hyperliquid read-only wrapper (M0).

Uses the Info endpoint of the official SDK. No orders, no withdrawals.
Provides:
- `read_mark_price(coin)`: current mark price for a perp.
- `read_position(coin)`: signed position size, entry, isolated margin.
- `read_funding_avg_30d(coin)`: annualized 30-day mean hourly funding.
- `read_maintenance_margin(coin)`: for coherence check vs config at boot.

Threading model: the official SDK is sync — we offload to a thread here.
The rest of the bot is asyncio; wrappers hide the sync/async gap.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

from hyperliquid.info import Info

from delta0.logging import get_logger

log = get_logger(__name__)

_HOURS_PER_YEAR = 24 * 365


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
        self._info = Info(api_url, skip_ws=True)

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    async def read_mark_price(self, coin: str) -> float:
        mids: dict[str, str] = await self._run(self._info.all_mids)
        if coin not in mids:
            raise KeyError(f"coin {coin!r} not found in Hyperliquid mids")
        return float(mids[coin])

    async def read_market_meta(self, coin: str) -> HLMarketMeta:
        meta: dict[str, Any] = await self._run(self._info.meta)
        universe: list[dict[str, Any]] = meta.get("universe", [])
        for entry in universe:
            if entry.get("name") == coin:
                # maxLeverage e.g. 50 -> maintenance ratio is HL-specific.
                # Hyperliquid publishes maintenance leverage; we approximate mm = 1/(2 * maxLev)
                # per public docs and re-verify against `clearinghouseState` at boot.
                max_lev = float(entry.get("maxLeverage", 0)) or 1.0
                mm = 1.0 / (2.0 * max_lev)
                mark = await self.read_mark_price(coin)
                return HLMarketMeta(coin=coin, mark_price=mark, maintenance_margin_ratio=mm)
        raise KeyError(f"coin {coin!r} not in universe")

    async def read_position(self, coin: str) -> HLPosition | None:
        state: dict[str, Any] = await self._run(self._info.user_state, self._user)
        asset_positions: list[dict[str, Any]] = state.get("assetPositions", [])
        for ap in asset_positions:
            pos: dict[str, Any] = ap.get("position", {})
            if pos.get("coin") == coin:
                size = float(pos.get("szi", 0.0))
                entry = float(pos.get("entryPx", 0.0))
                leverage_obj: dict[str, Any] = pos.get("leverage", {})
                lev_type = leverage_obj.get("type")
                lev_value = int(leverage_obj.get("value", 0))
                if lev_type != "isolated":
                    log.warning(
                        "hl_position_not_isolated",
                        message="position HL en marge cross — attendu: isolée",
                        coin=coin,
                        leverage_type=lev_type,
                    )
                margin = float(pos.get("marginUsed", 0.0))
                return HLPosition(
                    coin=coin,
                    size_signed=size,
                    entry_price=entry,
                    isolated_margin_usd=margin,
                    leverage=lev_value,
                )
        return None

    async def read_funding_avg_30d(self, coin: str) -> float:
        """Annualized 30-day average hourly funding for `coin`.

        Hyperliquid's `funding_history` returns hourly rates; we take the mean
        over the last 720 samples and annualize.
        """
        # 30 days back in ms.
        start_time_ms = self._now_ms() - 30 * 24 * 3600 * 1000
        history: list[dict[str, Any]] = await self._run(
            self._info.funding_history,
            coin,
            start_time_ms,
        )
        if not history:
            return 0.0
        rates: list[float] = [float(h["fundingRate"]) for h in history]
        mean_hourly = sum(rates) / len(rates)
        return mean_hourly * _HOURS_PER_YEAR

    async def read_last_hour_funding(self, coin: str) -> float:
        start_time_ms = self._now_ms() - 2 * 3600 * 1000
        history: list[dict[str, Any]] = await self._run(
            self._info.funding_history,
            coin,
            start_time_ms,
        )
        if not history:
            return 0.0
        return float(history[-1]["fundingRate"])

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    async def read_user_summary(self) -> dict[str, Any]:
        """Full clearinghouse state — useful for `status` command."""
        return cast(dict[str, Any], await self._run(self._info.user_state, self._user))
