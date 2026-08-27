"""Load and validate `config.yaml` into a frozen `Config`.

Env vars are read via pydantic-settings. Secrets never appear in the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from delta0.config.schema import Config


def load_config(path: str | Path) -> Config:
    """Load a YAML config file and return a validated, frozen `Config`.

    Raises `pydantic.ValidationError` on any invariant violation — do not catch.
    A bot booted on invalid config is a strictly worse outcome than a bot that
    refuses to start.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise TypeError(f"config root must be a mapping, got {type(raw).__name__}")

    return Config.model_validate(raw)
