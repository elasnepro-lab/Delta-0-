"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from delta0.config import Config, load_config


@pytest.fixture
def example_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml.example"


@pytest.fixture
def config(example_config_path: Path) -> Config:
    return load_config(example_config_path)
