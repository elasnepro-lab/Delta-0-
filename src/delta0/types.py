"""Domain types shared across the bot.

These are the values that flow between watcher -> decision -> executor.
Frozen by construction — the decision engine must be pure (README section 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Literal


class Priority(IntEnum):
    """Priorities of the decision table (README section 7).

    Lower value = higher priority. P1 is life-safety.
    """

    P1_LIQUIDATION_DETECTED = 1
    P2_EMERGENCY_REDUCE = 2
    P3_EMERGENCY_REPAY = 3
    P4_DELEVERAGE = 4
    P5_PUMP_UP = 5
    P6_PUMP_DOWN = 6
    P7_RECENTER = 7
    P8_DELTA_RETRUE = 8
    P9_SKIM = 9
    P10_REGIME = 10


ActionKind = Literal[
    "NOOP",
    "REDUCE",  # P2 IOC close a fraction of the short
    "REPAY_FROM_CUSHION",  # P3
    "STEPWISE_DELEVERAGE",  # P4
    "PUMP_UP",  # P5
    "PUMP_DOWN",  # P6
    "RECENTER_UP",  # P7
    "RECENTER_DOWN",  # P7
    "RETRUE_SHORT",  # P8
    "SKIM_RECOMPOSE",  # P9
    "REGIME_STEP",  # P10
    "LIQUIDATION_RESPONSE",  # P1
]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A single, dated observation of the world.

    Produced by the watcher, consumed by the pure decision engine.
    All amounts are in USD unless the field name says otherwise.
    Balances are in native token units (Decimal-ish) — kept as float for now,
    to be tightened to Decimal in M1 once the shape is stable.
    """

    ts: datetime

    # Aave leg.
    wsteth_atoken_balance: float
    wsteth_price_usd: float
    usdc_atoken_balance: float
    usdc_variable_debt_balance: float
    hf: float  # from Pool.getUserAccountData — trusted, never recomputed
    aave_lt_wsteth: float  # liquidation threshold, on-chain
    aave_ltv_max_wsteth: float  # max LTV allowed, on-chain
    aave_emode: int  # must be 0 in nominal operation

    # Hyperliquid leg.
    mark_price: float
    short_size_eth: float  # positive number, this is a short position
    isolated_margin_usd: float
    hl_maintenance_margin: float  # observed, compared to config
    funding_last_hour: float  # hourly rate
    funding_30d_annualized: float

    # Aave money-market rates.
    borrow_apr: float

    # Environment.
    gas_eth: float
    ws_last_tick_age_s: float
    rpc_ok: bool

    # --- Derived (see README section 5) ---------------------------------------

    @property
    def spot_usd(self) -> float:
        return self.wsteth_atoken_balance * self.wsteth_price_usd

    @property
    def cushion_usd(self) -> float:
        return self.usdc_atoken_balance

    @property
    def collateral_usd(self) -> float:
        return self.spot_usd + self.cushion_usd

    @property
    def debt_usd(self) -> float:
        return self.usdc_variable_debt_balance

    @property
    def ltv(self) -> float:
        if self.collateral_usd == 0.0:
            return 0.0
        return self.debt_usd / self.collateral_usd

    @property
    def notional_usd(self) -> float:
        return self.short_size_eth * self.mark_price

    @property
    def margin_ratio(self) -> float:
        if self.notional_usd == 0.0:
            return float("inf")
        return self.isolated_margin_usd / self.notional_usd

    @property
    def delta_usd(self) -> float:
        return self.spot_usd - self.notional_usd

    @property
    def delta_pct(self) -> float:
        if self.spot_usd == 0.0:
            return 0.0
        return self.delta_usd / self.spot_usd

    @property
    def carry_spread(self) -> float:
        return self.funding_30d_annualized - self.borrow_apr

    @property
    def equity(self) -> float:
        return self.collateral_usd + self.isolated_margin_usd - self.debt_usd


@dataclass(frozen=True, slots=True)
class TargetState:
    """Target values for BUILD, RECENTER, SKIM. See README sections 3 & 8."""

    spot_target_usd: float
    notional_target_usd: float
    margin_target_usd: float
    debt_target_usd: float


@dataclass(frozen=True, slots=True)
class Action:
    """A decision emitted by the pure engine.

    The executor consumes actions; the decision engine never executes them.
    """

    kind: ActionKind
    priority: Priority
    reason: str
    # Free-form typed payload — future subclasses can specialize.
    # We keep it a simple dict for now; the executor validates shape per kind.
    params: dict[str, float | int | str]


NOOP: Action = Action(
    kind="NOOP",
    priority=Priority.P10_REGIME,
    reason="no trigger fired",
    params={},
)
