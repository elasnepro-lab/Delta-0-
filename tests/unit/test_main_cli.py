"""CLI smoke tests — argument parsing and duration parsing."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from delta0.main import _parse_duration, app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "delta0" in result.stdout


def test_config_check_ok() -> None:
    result = runner.invoke(app, ["config-check", "-c", "config.yaml.example"])
    assert result.exit_code == 0
    assert "valide" in result.stdout


def test_config_check_bad_file() -> None:
    result = runner.invoke(app, ["config-check", "-c", "does/not/exist.yaml"])
    assert result.exit_code != 0


def test_parse_duration_seconds() -> None:
    assert _parse_duration("30s") == 30.0


def test_parse_duration_minutes() -> None:
    assert _parse_duration("2m") == 120.0


def test_parse_duration_hours() -> None:
    assert _parse_duration("2h") == 7200.0


def test_parse_duration_days() -> None:
    assert _parse_duration("7d") == 7 * 86400.0


def test_parse_duration_bare_number_is_seconds() -> None:
    assert _parse_duration("42") == 42.0


def test_parse_duration_invalid() -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017 - typer.BadParameter wraps this
        _parse_duration("nope")


def test_parse_duration_empty() -> None:
    with pytest.raises(Exception):  # noqa: PT011, B017
        _parse_duration("")
