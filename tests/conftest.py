"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from delta0.config import Config, load_config
from delta0.decision import BlindState, OperationalContext
from delta0.settings import Settings
from delta0.types import Snapshot
from tests.world import reference_snapshot

# Every env var `Settings` knows about. Cleared for the whole unit suite.
_SETTINGS_ENV_VARS = (
    "ARBITRUM_RPC_PRIMARY",
    "ARBITRUM_RPC_FALLBACK",
    "BOT_MASTER_ADDRESS",
    "BOT_MASTER_PRIVATE_KEY",
    "HL_AGENT_ADDRESS",
    "HL_AGENT_PRIVATE_KEY",
    "TG_TOKEN",
    "TG_CHAT",
)


@pytest.fixture(autouse=True)
def isolate_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cut the unit suite off from the operator's real `.env` and env vars.

    Without this, a test passes on a machine that has a populated `.env` and
    fails in CI, which has none — the failure mode that let three CLI tests
    through review. Unit tests must see the same empty environment CI does.
    Anything that genuinely needs settings should build a `Settings` object
    explicitly rather than rely on ambient state.
    """
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", str(tmp_path / "absent.env"))


@pytest.fixture
def example_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml.example"


@pytest.fixture
def config(example_config_path: Path) -> Config:
    return load_config(example_config_path)


# --- Le monde de reference ----------------------------------------------------
# 16 wstETH a 3 125 $ font 50 000 $ de spot et, au ratio de 1,25, 20 ETH
# d'equivalent face a un short de 20 ETH : delta nul. Le LT et le HF sont ceux
# d'Arbitrum, lus on-chain — voir memory/aave_findings.md §9.


@pytest.fixture
def now() -> datetime:
    # Monday 2026-08-24 10:00 UTC.
    return datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def anchor_price() -> float:
    return 2_500.0


@pytest.fixture
def stable_snapshot(now: datetime) -> Snapshot:
    """A snapshot exactly at target: nothing should trigger (see tests/world.py)."""
    return reference_snapshot(ts=now)


@pytest.fixture
def nominal_ctx(now: datetime, anchor_price: float) -> OperationalContext:
    return OperationalContext(
        now_utc=now,
        blind_state=BlindState.NOMINAL,
        liquidation_event=False,
        anchor_price=anchor_price,
        last_skim_at=None,
        desired_exposure_mult=2.5,
        current_exposure_mult=2.5,
    )
