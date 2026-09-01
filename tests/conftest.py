"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from delta0.config import Config, load_config
from delta0.settings import Settings

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
    "DELTA0_MODE",
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
