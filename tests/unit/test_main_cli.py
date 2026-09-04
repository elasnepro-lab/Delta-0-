"""CLI smoke tests — argument parsing and duration parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from delta0.main import _parse_duration, app
from delta0.settings import load_settings
from delta0.state import StateStore

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


async def _seed(db: Path, samples: dict[str, list[float]]) -> None:
    store = StateStore(db)
    await store.open()
    try:
        for path, values in samples.items():
            for v in values:
                await store.record_latency(path, v)
    finally:
        await store.close()


def test_report_on_empty_db_reports_aucun(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["report", "--db", str(tmp_path / "empty.db"), "-c", "config.yaml.example"],
    )
    assert result.exit_code == 0
    assert "Chemins critiques" in result.stdout
    assert "P1/P2" in result.stdout
    # No measurement must never render as a fast path.
    assert "AUCUN" in result.stdout
    assert "NON satisfait" in result.stdout


def test_report_renders_a_measured_path_within_budget(tmp_path: Path) -> None:
    db = tmp_path / "seeded.db"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0, 420.0, 510.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    assert "p1_p2_hl_order" in result.stdout


def test_report_flags_prudent_mode_when_p95_blows_the_budget(tmp_path: Path) -> None:
    db = tmp_path / "slow.db"
    # P1/P2 budget is 2 s; 5 s is past 2 s x 1.5.
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [5_000.0, 5_200.0, 5_400.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert result.exit_code == 0
    assert "PRUDENT" in result.stdout
    assert "Mode prudent" in result.stdout


def _config_with_dry_run(tmp_path: Path, *, dry_run: bool) -> Path:
    raw = Path("config.yaml.example").read_text(encoding="utf-8")
    flipped = raw.replace("  dry_run: true", f"  dry_run: {str(dry_run).lower()}")
    out = tmp_path / "config.yaml"
    out.write_text(flipped, encoding="utf-8")
    return out


def test_live_micro_ops_refused_while_dry_run_is_on() -> None:
    result = runner.invoke(
        app,
        ["tracer", "-c", "config.yaml.example", "--live-micro-ops", "-d", "1s"],
    )
    assert result.exit_code == 2
    assert "REFUS" in result.stdout


def test_rehearse_and_live_micro_ops_are_mutually_exclusive() -> None:
    result = runner.invoke(
        app,
        ["tracer", "-c", "config.yaml.example", "--rehearse", "--live-micro-ops", "-d", "1s"],
    )
    assert result.exit_code == 6
    assert "s'excluent" in result.stdout


def test_rehearse_refused_when_dry_run_is_off(tmp_path: Path) -> None:
    cfg = _config_with_dry_run(tmp_path, dry_run=False)
    result = runner.invoke(app, ["tracer", "-c", str(cfg), "--rehearse", "-d", "1s"])
    assert result.exit_code == 6
    assert "REFUS" in result.stdout


def test_unit_suite_cannot_see_the_operator_env() -> None:
    """Guard on the conftest isolation fixture itself.

    If this starts passing a populated Settings back, the suite has regained
    access to a real `.env` and every CLI test that depends on env absence
    becomes machine-dependent.
    """
    with pytest.raises(ValidationError):
        load_settings()
