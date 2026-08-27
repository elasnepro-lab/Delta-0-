"""Config loading and invariant checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from delta0.config import Config, RuntimeMode, SkimPolicy, load_config


def test_example_config_loads(example_config_path: Path) -> None:
    cfg = load_config(example_config_path)
    assert cfg.capital_usd == pytest.approx(20_000.0)
    assert cfg.short_leverage == 10
    assert cfg.target_ltv == pytest.approx(0.70)
    assert cfg.skim_policy is SkimPolicy.RECOMPOSE
    assert cfg.mode is RuntimeMode.DRY_RUN


def test_exposure_mult_matches_formula(config: Config) -> None:
    expected = 1.0 / (1.0 - config.target_ltv + 1.0 / config.short_leverage)
    assert config.exposure_mult == pytest.approx(expected, abs=1e-6)


def test_target_margin_matches_leverage(config: Config) -> None:
    assert config.target_margin_ratio == pytest.approx(1.0 / config.short_leverage)


def test_reject_usdc_e(example_config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(example_config_path.read_text())
    raw["venues"]["usdc_address"] = "0xFF970A61A04B1CA14834A43F5DE4533EBDDB5CC8"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match=r"USDC\.e is forbidden"):
        load_config(bad)


def test_reject_bad_exposure_mult(example_config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(example_config_path.read_text())
    raw["exposure_mult"] = 3.0  # inconsistent
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="exposure_mult mismatch"):
        load_config(bad)


def test_reject_reduce_above_pump(example_config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(example_config_path.read_text())
    raw["emergency"]["margin_ratio_reduce"] = 0.06  # above pump 0.05
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="margin_ratio_reduce must be strictly lower"):
        load_config(bad)


def test_reject_ltv_order(example_config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(example_config_path.read_text())
    raw["emergency"]["ltv_pump"] = 0.80  # above cushion 0.79
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="ltv_pump < ltv_cushion < ltv_deleverage"):
        load_config(bad)


def test_reject_cushion_floor_above_target(example_config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(example_config_path.read_text())
    raw["cushion_floor_pct"] = 0.06  # above cushion_pct 0.05
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="cushion_floor_pct must be strictly lower"):
        load_config(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_non_mapping_root_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(TypeError, match="config root must be a mapping"):
        load_config(bad)
