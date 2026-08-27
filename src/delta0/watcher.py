"""Watcher — produces `Snapshot`s from live venues.

M1 scope: async polling of Aave and Hyperliquid read-only APIs, assembly of
one dated Snapshot per tick. If a `HyperliquidStream` is attached, the WS
mark price is preferred over the REST snapshot (fresher) and each WS tick
resets the watchdog's freshness counter.

The watcher does NOT decide. It just observes. See README §3.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from delta0.config import Config
from delta0.logging import get_logger
from delta0.types import Snapshot
from delta0.venues.aave import AaveReader
from delta0.venues.hl_stream import HyperliquidStream
from delta0.venues.hyperliquid import HyperliquidReader
from delta0.watchdog import Watchdog

log = get_logger(__name__)


class WatcherProtocol(Protocol):
    """Anything that can produce a snapshot on demand."""

    async def snapshot(self) -> Snapshot: ...


@dataclass(slots=True)
class LiveWatcher:
    """Live watcher wrapping the M0 read-only clients.

    Marks the watchdog on every call: `mark_*_ok` on success,
    `mark_*_failure` on any exception. The watchdog handles the transition to
    BLIND if failures accumulate.

    When `stream` is provided, the WS mark price overrides the REST value and
    every arriving tick resets the watchdog's freshness counter — that is the
    whole point of the WS feed (READ §11).
    """

    config: Config
    aave: AaveReader
    hl: HyperliquidReader
    watchdog: Watchdog
    coin: str = "ETH"
    stream: HyperliquidStream | None = None

    async def snapshot(self) -> Snapshot:
        now_utc = datetime.now(UTC)
        now_mono = time.monotonic()

        # --- Aave leg ---------------------------------------------------------
        try:
            account = await self.aave.read_account_data()
            wsteth = await self.aave.read_token_balances(self.config.venues.wsteth_address)
            usdc = await self.aave.read_token_balances(self.config.venues.usdc_address)
            usdc_rates = await self.aave.read_reserve_rates(self.config.venues.usdc_address)
            gas = await self.aave.read_gas_balance_eth()
            self.watchdog.mark_aave_ok(now=now_mono)
        except Exception:
            self.watchdog.mark_aave_failure()
            log.exception("aave_read_failed", message="lecture Aave en échec")
            raise

        # --- HL leg -----------------------------------------------------------
        try:
            meta = await self.hl.read_market_meta(self.coin)
            position = await self.hl.read_position(self.coin)
            funding_30d = await self.hl.read_funding_avg_30d(self.coin)
            funding_1h = await self.hl.read_last_hour_funding(self.coin)
            self.watchdog.mark_hl_ok(now=now_mono)
            # WS ticks (if any) refresh the freshness signal. Without a stream,
            # a successful REST poll still counts as "fresh".
            latest_tick = self.stream.try_latest_mark() if self.stream is not None else None
            if latest_tick is not None:
                self.watchdog.mark_ws_tick(now=latest_tick.ts_monotonic)
                mark_price = latest_tick.mark_price
            else:
                self.watchdog.mark_ws_tick(now=now_mono)
                mark_price = meta.mark_price
        except Exception:
            self.watchdog.mark_hl_failure()
            log.exception("hl_read_failed", message="lecture Hyperliquid en échec")
            raise

        # Short size in ETH: for HL, a short position has a negative szi;
        # we normalize to a positive magnitude — the sign is implicit in the
        # "short" role. If the position is missing, size is 0 (never built yet).
        short_size = abs(position.size_signed) if position is not None else 0.0
        margin = position.isolated_margin_usd if position is not None else 0.0

        return Snapshot(
            ts=now_utc,
            wsteth_atoken_balance=wsteth.atoken_balance,
            # wstETH ≈ ETH for M1; refine with wstETH/stETH rate in M1-B.
            wsteth_price_usd=mark_price,
            usdc_atoken_balance=usdc.atoken_balance,
            usdc_variable_debt_balance=usdc.variable_debt_balance,
            hf=account.health_factor,
            aave_lt_wsteth=account.liquidation_threshold,
            aave_ltv_max_wsteth=account.ltv_max,
            aave_emode=account.emode,
            mark_price=mark_price,
            short_size_eth=short_size,
            isolated_margin_usd=margin,
            hl_maintenance_margin=meta.maintenance_margin_ratio,
            funding_last_hour=funding_1h,
            funding_30d_annualized=funding_30d,
            borrow_apr=usdc_rates.variable_borrow_apr,
            gas_eth=gas,
            ws_last_tick_age_s=self.watchdog.ws_stale_seconds(now=now_mono),
            rpc_ok=True,
        )
