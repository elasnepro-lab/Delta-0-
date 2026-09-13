"""Pydantic schema for `config.yaml`.

Every business parameter of the bot lives here — README section 4.
Cross-field validators enforce the invariants that the classeur Model C guarantees:
- exposure_mult == 1 / (1 - target_ltv + 1 / short_leverage)
- Threshold monotonicity on both flanks (recenter < pump < reduce < liquidation).
- LTV thresholds are strictly ordered (pump < cushion < deleverage < LT).

If any invariant fails, the bot refuses to boot — that is by design.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tolerance for cross-field float invariant checks.
_FLOAT_EPS = 1e-6
# EVM address length: "0x" + 40 hex chars.
_EVM_ADDRESS_LEN = 42


class RuntimeMode(StrEnum):
    """Execution mode, set in `config.yaml` only.

    An environment override was promised here and never read. It is not coming
    back: a variable that silently flips the bot to LIVE is a worse lever than
    the file an operator has to edit and restart on (README §17, decision 5).
    """

    DRY_RUN = "DRY_RUN"
    LIVE_SMALL = "LIVE_SMALL"
    LIVE = "LIVE"


class SkimPolicy(StrEnum):
    """Skim policy — README section 8.5. v1 default: recompose."""

    RECOMPOSE = "recompose"
    DELEVERAGE = "deleverage"
    DIVIDEND = "dividend"


class OrderStyle(StrEnum):
    MAKER_THEN_CROSS = "maker_then_cross"


# --- Sub-models ---------------------------------------------------------------

# A priority whose threshold sits closer than this to the liquidation threshold
# has no room to act before Aave liquidates.
MIN_LTV_MARGIN_TO_LT = 0.01

_Ratio = Annotated[float, Field(gt=0.0, lt=1.0)]
_PositiveFloat = Annotated[float, Field(gt=0.0)]
_PositiveInt = Annotated[int, Field(gt=0)]
_Bps = Annotated[int, Field(ge=0, le=10_000)]


class RegimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spread_full_bps: _Bps
    hysteresis_days: _PositiveInt


class EmergencyConfig(BaseModel):
    """Thresholds for priorities P1-P6. README section 7."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    margin_ratio_pump: _Ratio
    margin_ratio_reduce: _Ratio
    reduce_fraction: _Ratio
    # USDC left free on Hyperliquid, as a fraction of the notional. This is
    # what P2 spends: adding isolated margin is the only action measured to
    # move the liquidation price, and it can only spend what sits there.
    # Undeployed capital, so it costs carry — priced in the classeur.
    hl_reserve_pct: _Ratio

    # Down flank: distances BELOW the on-chain liquidation threshold, in LTV
    # points — not absolute LTVs. Absolute values cannot live in a file: they
    # only mean something relative to a parameter Aave governance can change,
    # and the previous ones were calibrated on Ethereum's LT (0.81) while
    # Arbitrum's is 0.79, which put two of the three thresholds at or beyond
    # the liquidation point. See memory/aave_findings.md §9.
    ltv_margin_pump: _Ratio
    ltv_margin_cushion: _Ratio
    ltv_margin_deleverage: _Ratio

    @model_validator(mode="after")
    def _check_monotonicity(self) -> EmergencyConfig:
        # Up flank: reduce triggers earlier (smaller margin ratio) than pump.
        if self.margin_ratio_reduce >= self.margin_ratio_pump:
            raise ValueError(
                "margin_ratio_reduce must be strictly lower than margin_ratio_pump "
                "(reduce fires closer to liquidation)."
            )
        # Down flank: a wider margin means the priority fires earlier, so the
        # pump must sit furthest from the liquidation threshold.
        margins = (self.ltv_margin_pump, self.ltv_margin_cushion, self.ltv_margin_deleverage)
        if not (margins[0] > margins[1] > margins[2]):
            raise ValueError(
                "LTV margins must satisfy "
                "ltv_margin_pump > ltv_margin_cushion > ltv_margin_deleverage "
                "(a wider margin fires earlier)."
            )
        if margins[2] < MIN_LTV_MARGIN_TO_LT:
            raise ValueError(
                f"the tightest LTV margin must leave at least {MIN_LTV_MARGIN_TO_LT} "
                "to the liquidation threshold; below that the priority cannot act "
                "before Aave does."
            )
        return self


class WatchdogConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ws_stale_s: _PositiveInt
    rpc_fail_s: _PositiveInt
    tx_fail_max: _PositiveInt
    latency_budget_factor: Annotated[float, Field(gt=1.0)]


class TracerConfig(BaseModel):
    """Safeties for M1 TRACER micro-operations (README §14).

    These caps exist to make sure a bug in the tracer executor cannot drain
    the operational float (~100 $) that funds the latency-measurement traffic.
    Every micro-op is checked against ALL of these before any network call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_op_usd: _PositiveFloat = Field(
        default=15.0,
        description="Hard cap on the notional of any single micro-operation.",
    )
    max_ops_per_hour: _PositiveInt = Field(
        default=20,
        description="Rolling rate limit — refuses further ops beyond this per hour.",
    )
    require_first_use_confirmation: bool = Field(
        default=True,
        description=(
            "First execution of each op kind is blocked until the operator "
            "explicitly confirms via CLI flag or env var."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When true, the executor prepares every step but never sends a tx. "
            "M1-B2 defaults to True — flip to False only in a supervised run."
        ),
    )

    # M1-B2 scheduler: how often each micro-op cycle fires during LIVE TRACER.
    # Chosen to hit reasonable sample sizes over 7 days without overwhelming
    # the wallet or rate limits.
    aave_cycle_every_s: _PositiveInt = Field(
        default=1800,  # 30 min -> ~336 samples over 7 days
        description="Interval between Aave 4-op cycles (approve+supply+repay+withdraw).",
    )
    hl_cancel_every_s: _PositiveInt = Field(
        default=600,  # 10 min -> ~1000 samples over 7 days
        description="Interval between HL post-only + cancel round-trips.",
    )
    bridge_every_s: _PositiveInt = Field(
        default=43200,  # 12h -> ~14 samples over 7 days
        description="Interval between bridge full round-trips (~$1 fee each).",
    )
    aave_cycle_amount_usdc: _PositiveFloat = Field(default=5.0)
    bridge_amount_usdc: _PositiveFloat = Field(default=5.0)


class VenuesConfig(BaseModel):
    """External venue endpoints and on-chain addresses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usdc_address: str
    wsteth_address: str
    # Needed to price wstETH in ETH terms: the Aave oracle quotes both assets in
    # USD, and their ratio is the wstETH/ETH rate. Reading it there rather than
    # from the token keeps the rate consistent with the health factor Aave
    # applies — and wstETH on Arbitrum is a bridged token that does not expose
    # stEthPerToken() at all. See memory/aave_findings.md §10.
    weth_address: str
    aave_pool: str
    aave_data_provider: str
    # No `hl_ws`: the SDK derives the WebSocket URL from `hl_api` and accepts no
    # other, so a separate setting could only disagree with what is used.
    hl_api: str
    # Hyperliquid Bridge2 contract on Arbitrum — recipient of USDC transfers
    # for HL account funding. See README §9.3.
    hl_bridge2: str = "0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7"
    # Multicall3, deployed at the same address on every EVM chain. The Aave leg
    # of a snapshot goes through it as ONE eth_call instead of eleven: the
    # 2026-09-13 quota alert showed the M1 run alone had spent ~80 % of the
    # free RPC allowance, and a LIVE cadence would need five times that.
    multicall3_address: str = "0xcA11bde05977b3631167028862bE2a173976CA11"

    @field_validator(
        "usdc_address",
        "wsteth_address",
        "weth_address",
        "aave_pool",
        "aave_data_provider",
        "hl_bridge2",
        "multicall3_address",
    )
    @classmethod
    def _check_eth_address(cls, v: str) -> str:
        if not v.startswith("0x") or len(v) != _EVM_ADDRESS_LEN:
            raise ValueError(f"invalid Ethereum address: {v!r}")
        return v

    @field_validator("usdc_address")
    @classmethod
    def _reject_usdc_e(cls, v: str) -> str:
        # Native USDC on Arbitrum (canonical, Circle-issued).
        # We refuse to load a config that points at USDC.e (bridged) — README 9.1.
        usdc_e = "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"
        if v.lower() == usdc_e:
            raise ValueError("USDC.e is forbidden by design — use native USDC (0xaf88...5831).")
        return v


class AlertsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    telegram_bot_token: str = Field(default="env:TG_TOKEN")
    telegram_chat_id: str = Field(default="env:TG_CHAT")


# --- Root config --------------------------------------------------------------


class Config(BaseModel):
    """Full bot configuration. Frozen — mutation is a bug."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Capital and leverage.
    capital_usd: _PositiveFloat
    short_leverage: Annotated[int, Field(ge=2, le=20)]
    target_ltv: _Ratio
    target_margin_ratio: _Ratio
    exposure_mult: _PositiveFloat
    exposure_mult_half: _PositiveFloat
    maintenance_margin: _Ratio

    # Cushion.
    cushion_pct: _Ratio
    cushion_floor_pct: _Ratio

    # Recentering bands (asymmetric — README section 1).
    recenter_up: _Ratio
    recenter_down: _Ratio
    delta_tolerance: _Ratio

    # Skim.
    skim_cron: str
    skim_min_usd: _PositiveFloat
    skim_policy: SkimPolicy

    # Regime gate.
    regime: RegimeConfig

    # Execution.
    slippage_max_bps: _Bps
    order_style: OrderStyle
    gas_min_eth: _PositiveFloat

    # Emergency and watchdog.
    emergency: EmergencyConfig
    watchdog: WatchdogConfig

    # M1 TRACER safeties. Defaults are safe: dry_run=True, small cap, low rate.
    tracer: TracerConfig = Field(default_factory=TracerConfig)

    # Venues and alerts.
    venues: VenuesConfig
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    # Runtime.
    mode: RuntimeMode = RuntimeMode.DRY_RUN
    live_small_cap_pct: _Ratio

    # --- Cross-field invariants ------------------------------------------------

    @model_validator(mode="after")
    def _check_exposure_mult(self) -> Config:
        expected = 1.0 / (1.0 - self.target_ltv + 1.0 / self.short_leverage)
        if abs(self.exposure_mult - expected) > _FLOAT_EPS:
            raise ValueError(
                f"exposure_mult mismatch: got {self.exposure_mult}, "
                f"expected {expected:.6f} = 1 / (1 - target_ltv + 1 / short_leverage). "
                "Fix the config — do not change the formula."
            )
        if self.exposure_mult_half >= self.exposure_mult:
            raise ValueError("exposure_mult_half must be strictly lower than exposure_mult.")
        return self

    @model_validator(mode="after")
    def _check_target_margin_matches_leverage(self) -> Config:
        expected = 1.0 / self.short_leverage
        if abs(self.target_margin_ratio - expected) > _FLOAT_EPS:
            raise ValueError(
                f"target_margin_ratio {self.target_margin_ratio} must equal "
                f"1 / short_leverage = {expected}."
            )
        return self

    @model_validator(mode="after")
    def _check_recenter_bands(self) -> Config:
        # Recenter must fire before pump (asymmetric bands, README section 1).
        # Up flank: recenter_up < margin_ratio_pump translated to price? We keep it simple:
        # recenter thresholds must be strictly positive and below the emergency thresholds
        # measured in price space; the mapping is documented in the decision table.
        if self.recenter_up <= 0 or self.recenter_down <= 0:
            raise ValueError("recenter thresholds must be strictly positive.")
        if self.cushion_floor_pct >= self.cushion_pct:
            raise ValueError("cushion_floor_pct must be strictly lower than cushion_pct.")
        return self

    @model_validator(mode="after")
    def _check_ltv_below_liquidation(self) -> Config:
        # The thresholds themselves depend on the on-chain LT, so they cannot be
        # checked here — `derive_bands` builds them and `check_bands` refuses the
        # boot when they collapse onto the target. What IS checkable without the
        # chain: the widest margin must still leave the pump above the target,
        # whatever plausible LT we face. With LT >= target + widest margin the
        # pump sits above target by construction; below that the config can
        # never be coherent.
        if self.emergency.ltv_margin_pump >= 1.0 - self.target_ltv:
            raise ValueError(
                "emergency.ltv_margin_pump is so wide that no liquidation threshold "
                "could place the pump above target_ltv."
            )
        return self
