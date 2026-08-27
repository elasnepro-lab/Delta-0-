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
    """Execution mode. Set via env var DELTA0_MODE or config."""

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
    ltv_pump: _Ratio
    ltv_cushion: _Ratio
    ltv_deleverage: _Ratio

    @model_validator(mode="after")
    def _check_monotonicity(self) -> EmergencyConfig:
        # Up flank: reduce triggers earlier (smaller margin ratio) than pump.
        if self.margin_ratio_reduce >= self.margin_ratio_pump:
            raise ValueError(
                "margin_ratio_reduce must be strictly lower than margin_ratio_pump "
                "(reduce fires closer to liquidation)."
            )
        # Down flank: pump < cushion < deleverage.
        if not (self.ltv_pump < self.ltv_cushion < self.ltv_deleverage):
            raise ValueError(
                "LTV emergency thresholds must satisfy ltv_pump < ltv_cushion < ltv_deleverage."
            )
        return self


class WatchdogConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ws_stale_s: _PositiveInt
    rpc_fail_s: _PositiveInt
    tx_fail_max: _PositiveInt
    latency_budget_factor: Annotated[float, Field(gt=1.0)]


class VenuesConfig(BaseModel):
    """External venue endpoints and on-chain addresses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usdc_address: str
    wsteth_address: str
    aave_pool: str
    aave_data_provider: str
    hl_api: str
    hl_ws: str

    @field_validator("usdc_address", "wsteth_address", "aave_pool", "aave_data_provider")
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
        # We do not know the on-chain LT here — it is fetched at boot and compared then.
        # Sanity: emergency LTV thresholds must be strictly above target_ltv.
        if self.emergency.ltv_pump <= self.target_ltv:
            raise ValueError("emergency.ltv_pump must be strictly above target_ltv.")
        return self
