"""Replay a dated price path through the pure decision engine.

Not a backtest: no fees, no slippage, no order book. It answers one question
the classeur cannot — *do the defences arrive in time?* A band says where the
liquidation sits; it says nothing about whether an eight-minute pump can cross
a two-minute crash.

Actions therefore land after their measured latency rather than instantly, and
a position can be liquidated while an action is in flight. Those latencies come
from the M1 dry run (see memory and the run database), so the harness inherits
whatever the real paths cost.

Deliberately NOT modelled, so results stay readable as an upper bound on how
well the design behaves:
- fees, slippage and partial fills
- the venue refusing an action
- an action failing and being retried

This file is a helper, not a test module — `simulator.py` is outside pytest's
`test_*` collection pattern on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from delta0.config import Config
from delta0.decision import BlindState, OperationalContext, decide
from delta0.types import Action, Priority, Snapshot

# Seconds between a decision and its effect landing, p95 from the M1 run.
# `path.p6_bridge_down` was measured at 315 563 ms; the Aave legs around 1.3 s;
# an HL order at 958 ms. STEPWISE_DELEVERAGE has never been measured — it is a
# withdraw/swap/repay loop, and 60 s is a deliberately optimistic placeholder
# flagged as such rather than left silent.
ACTION_LATENCY_S: dict[str, float] = {
    "ADD_ISOLATED_MARGIN": 1.0,
    "REDUCE": 1.0,
    "REPAY_FROM_CUSHION": 1.3,
    "STEPWISE_DELEVERAGE": 60.0,
    "PUMP_UP": 11.2,
    "PUMP_DOWN": 316.0,
    "RETRUE_SHORT": 1.0,
    "RECENTER_UP": 316.0,
    "RECENTER_DOWN": 316.0,
    "LIQUIDATION_RESPONSE": 1.0,
    "SKIM_RECOMPOSE": 316.0,
    "REGIME_STEP": 316.0,
}


@dataclass
class Position:
    """Mutable balance sheet the simulator walks forward."""

    wsteth: float
    cushion_usd: float
    debt_usd: float
    short_eth: float
    margin_usd: float
    hl_free_usdc: float
    lt: float
    short_entry_px: float = 2_500.0
    wsteth_eth_ratio: float = 1.25

    def unrealized_pnl(self, eth_price: float) -> float:
        """A short gains when the price falls and bleeds when it rises."""
        return self.short_eth * (self.short_entry_px - eth_price)

    def effective_margin(self, eth_price: float) -> float:
        """Isolated margin net of the position's PnL — what the venue sees.

        Leaving the PnL out was the harness's own blind spot: without it the
        margin ratio never moved with the price, so the up flank could not fire
        at all and the reserve looked useless.
        """
        return max(0.0, self.margin_usd + self.unrealized_pnl(eth_price))

    def snapshot(self, eth_price: float, ts: datetime) -> Snapshot:
        wsteth_price = eth_price * self.wsteth_eth_ratio
        collateral = self.wsteth * wsteth_price + self.cushion_usd
        hf = float("inf") if self.debt_usd <= 0 else self.lt * collateral / self.debt_usd
        return Snapshot(
            ts=ts,
            wsteth_atoken_balance=self.wsteth,
            wsteth_price_usd=wsteth_price,
            wsteth_eth_ratio=self.wsteth_eth_ratio,
            usdc_atoken_balance=self.cushion_usd,
            usdc_variable_debt_balance=self.debt_usd,
            hf=hf,
            aave_lt_wsteth=self.lt,
            aave_ltv_max_wsteth=0.75,
            aave_emode=0,
            mark_price=eth_price,
            short_size_eth=self.short_eth,
            isolated_margin_usd=self.effective_margin(eth_price),
            hl_free_usdc=self.hl_free_usdc,
            hl_maintenance_margin=0.02,
            funding_last_hour=1.25e-5,
            funding_30d_annualized=0.11,
            borrow_apr=0.05,
            gas_eth=0.01,
            ws_last_tick_age_s=1.0,
            rpc_ok=True,
        )


def apply_action(position: Position, action: Action, eth_price: float) -> None:
    """Land an action on the balance sheet. Effects only, no cost model."""
    params = action.params
    kind = action.kind

    if kind == "ADD_ISOLATED_MARGIN":
        amount = min(float(params["add_margin_amount_usdc"]), position.hl_free_usdc)
        position.margin_usd += amount
        position.hl_free_usdc -= amount

    elif kind == "REDUCE":
        # The venue releases margin in proportion to the size closed, which is
        # exactly why this does not save the position. See hl_findings §10.
        fraction = float(params["close_fraction"])
        released = position.effective_margin(eth_price) * fraction
        position.margin_usd -= position.margin_usd * fraction
        position.short_eth *= 1 - fraction
        position.hl_free_usdc += released

    elif kind == "REPAY_FROM_CUSHION":
        amount = min(float(params["repay_amount_usdc"]), position.cushion_usd)
        position.debt_usd -= amount
        position.cushion_usd -= amount

    elif kind == "STEPWISE_DELEVERAGE":
        # Sell collateral, repay with the proceeds, until the target is met.
        target_ltv = float(params["target_ltv_after"])
        wsteth_price = eth_price * position.wsteth_eth_ratio
        collateral = position.wsteth * wsteth_price + position.cushion_usd
        excess = position.debt_usd - target_ltv * collateral
        if excess > 0:
            sold = min(excess / wsteth_price, position.wsteth)
            position.wsteth -= sold
            position.debt_usd -= sold * wsteth_price

    elif kind == "PUMP_UP":
        # Borrow on Aave, bridge, add as margin: debt grows, margin grows.
        amount = float(params["add_margin_amount_usdc"])
        position.debt_usd += amount
        position.margin_usd += amount

    elif kind == "PUMP_DOWN":
        # Withdraw from HL, bridge, repay: margin shrinks, debt shrinks.
        amount = min(float(params["repay_amount_usdc"]), position.margin_usd)
        position.margin_usd -= amount
        position.debt_usd -= amount

    elif kind in ("RETRUE_SHORT", "LIQUIDATION_RESPONSE"):
        # Re-sizing realises the PnL carried by the old size and re-bases.
        position.margin_usd = position.effective_margin(eth_price)
        position.short_entry_px = eth_price
        position.short_eth = float(params["target_short_size_eth"])


@dataclass
class PathResult:
    liquidated_at_s: float | None = None
    hf_min: float = float("inf")
    margin_ratio_min: float = float("inf")
    actions: list[tuple[float, Priority, str]] = field(default_factory=list)

    @property
    def survived(self) -> bool:
        return self.liquidated_at_s is None

    def count(self, kind: str) -> int:
        return sum(1 for _, _, k in self.actions if k == kind)


def run_path(
    prices: list[float],
    position: Position,
    config: Config,
    *,
    cadence_s: float = 5.0,
    one_in_flight: bool = True,
    anchored: bool = True,
    preempt: bool = False,
) -> PathResult:
    """Walk `prices` at `cadence_s`, deciding and landing actions with latency.

    `one_in_flight` mirrors invariant I7 (a single execution at a time). Turning
    it off is how the harness shows what the refire lock of chantier 3.1 is
    worth.

    `preempt=True` lets a strictly higher priority cancel one already in
    flight. Off by default because that is today's behaviour: the scheduler
    awaits each action, so a slow one holds the floor. README §6 says "les
    urgences préemptent tout" — it is a sentence, not code, and the difference
    is what chantier 3.5 buys.

    `anchored=False` drops the recentering anchor, as during INIT and BUILD.
    Worth knowing why it exists: with an anchor, P7 quietly absorbs moderate
    moves and the emergency ladder never runs, which is reassuring in
    production and useless for testing that ladder. Dropping it isolates the
    defences the bands were sized for.
    """
    result = PathResult()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    pending: list[tuple[float, Action]] = []

    for step, price in enumerate(prices):
        now_s = step * cadence_s

        # Land whatever has arrived.
        still_pending: list[tuple[float, Action]] = []
        for ready_at, action in pending:
            if ready_at <= now_s:
                apply_action(position, action, price)
            else:
                still_pending.append((ready_at, action))
        pending = still_pending

        snap = position.snapshot(price, start + timedelta(seconds=now_s))
        result.hf_min = min(result.hf_min, snap.hf)
        if snap.notional_usd > 0:
            result.margin_ratio_min = min(result.margin_ratio_min, snap.margin_ratio)

        if snap.hf <= 1.0 and result.liquidated_at_s is None:
            result.liquidated_at_s = now_s
            return result

        if one_in_flight and pending and not preempt:
            continue

        ctx = OperationalContext(
            now_utc=snap.ts,
            blind_state=BlindState.NOMINAL,
            anchor_price=prices[0] if anchored else None,
            last_skim_at=start,
            current_exposure_mult=config.exposure_mult,
            desired_exposure_mult=config.exposure_mult,
        )
        action = decide(snap, config, ctx)
        if action.kind == "NOOP":
            continue
        if preempt and pending:
            in_flight = min(a.priority for _, a in pending)
            if action.priority >= in_flight:
                continue
            # Strictly more urgent: drop what was in flight and take the floor.
            pending = []
        result.actions.append((now_s, action.priority, action.kind))
        pending.append((now_s + ACTION_LATENCY_S.get(action.kind, 1.0), action))

    return result


def linear_drop(pct: float, duration_s: float, cadence_s: float = 5.0) -> list[float]:
    """A straight fall of `pct` (0.15 for -15 %) spread over `duration_s`."""
    steps = max(1, int(duration_s / cadence_s))
    return [2_500.0 * (1 - pct * i / steps) for i in range(steps + 1)]


def reference_position(lt: float = 0.79) -> Position:
    """The chassis of README §4, at rest: 16 wstETH, 20 ETH short, 2 % reserve."""
    return Position(
        wsteth=16.0,
        cushion_usd=1_000.0,
        debt_usd=35_000.0,
        short_eth=20.0,
        margin_usd=5_000.0,
        hl_free_usdc=1_000.0,
        lt=lt,
    )
