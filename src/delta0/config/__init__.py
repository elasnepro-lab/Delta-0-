"""Typed configuration for the bot. See README section 4."""

from __future__ import annotations

from delta0.config.loader import load_config
from delta0.config.schema import (
    AlertsConfig,
    Config,
    EmergencyConfig,
    RegimeConfig,
    RuntimeMode,
    SkimPolicy,
    VenuesConfig,
    WatchdogConfig,
)

__all__ = [
    "AlertsConfig",
    "Config",
    "EmergencyConfig",
    "RegimeConfig",
    "RuntimeMode",
    "SkimPolicy",
    "VenuesConfig",
    "WatchdogConfig",
    "load_config",
]
