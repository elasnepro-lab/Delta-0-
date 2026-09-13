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
    "ADD_ISOLATED_MARGIN",  # P2 — local, one request, actually moves liquidationPx
    "REDUCE",  # P2 fallback — damage limitation only, see decision._p2
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
    wsteth_price_usd: float  # Aave oracle price — the one behind the health factor
    wsteth_eth_ratio: float  # ETH per wstETH, from the same oracle
    usdc_atoken_balance: float
    usdc_variable_debt_balance: float
    usdc_wallet_balance: float  # libre dans le portefeuille, ce qu'une op dépense
    hf: float  # from Pool.getUserAccountData — trusted, never recomputed
    aave_lt_wsteth: float  # liquidation threshold, on-chain
    aave_ltv_max_wsteth: float  # max LTV allowed, on-chain
    aave_emode: int  # must be 0 in nominal operation

    # Hyperliquid leg.
    mark_price: float
    short_size_eth: float  # positive number, this is a short position
    isolated_margin_usd: float
    hl_free_usdc: float  # on the account, not committed as margin
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
    def spot_eth_equivalent(self) -> float:
        """The collateral expressed in ETH — what the short has to match.

        The hedge is a short on ETH, so neutrality is an equality of ETH
        quantities, not of dollar amounts. Stating it this way makes the delta
        immune to the USD base: a stETH depeg moves the Aave leg's LTV, which
        is an Aave problem, and leaves this figure alone. See README §5.
        """
        return self.wsteth_atoken_balance * self.wsteth_eth_ratio

    @property
    def delta_eth(self) -> float:
        """Uncovered ETH exposure. Positive means long, negative means short."""
        return self.spot_eth_equivalent - self.short_size_eth

    @property
    def delta_pct(self) -> float:
        if self.spot_eth_equivalent == 0.0:
            return 0.0
        return self.delta_eth / self.spot_eth_equivalent

    @property
    def delta_usd(self) -> float:
        """Reporting only — decisions use `delta_eth` / `delta_pct`.

        Kept because a dollar figure reads better in a digest, but it mixes two
        price sources (oracle for the spot, mark for the notional) and so drifts
        from `delta_eth` whenever they diverge.
        """
        return self.spot_usd - self.notional_usd

    @property
    def carry_spread(self) -> float:
        return self.funding_30d_annualized - self.borrow_apr

    @property
    def equity(self) -> float:
        return equity_usd(
            collateral_usd=self.collateral_usd,
            isolated_margin_usd=self.isolated_margin_usd,
            wallet_usdc=self.usdc_wallet_balance,
            hl_free_usdc=self.hl_free_usdc,
            debt_usd=self.debt_usd,
        )


def equity_usd(
    *,
    collateral_usd: float,
    isolated_margin_usd: float,
    wallet_usdc: float,
    hl_free_usdc: float,
    debt_usd: float,
) -> float:
    """What the machine is worth, in one place — README §5.

    The free balances count: they are dollars owned. The status panel added
    them after announcing "equity 0.00 $" with 169.80 $ on hand, while
    `Snapshot.equity` kept the older formula, so the panel and the engine
    disagreed on the number the solver sizes everything from. The HL free
    balance is also where the reserve lives: leaving it out would make the
    solver's own fixed point unreachable.
    """
    return collateral_usd + isolated_margin_usd + wallet_usdc + hl_free_usdc - debt_usd


@dataclass(frozen=True, slots=True)
class TargetState:
    """Target values for BUILD, RECENTER, SKIM. See README sections 3 & 8."""

    spot_target_usd: float
    notional_target_usd: float
    margin_target_usd: float
    reserve_target_usd: float  # free USDC on HL, what P2 pours into the margin
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
