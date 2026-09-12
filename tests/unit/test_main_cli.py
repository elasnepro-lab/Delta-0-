"""CLI smoke tests — argument parsing and duration parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from delta0.main import _parse_duration, app, resolve_root, resolve_under_root
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


def test_root_defaults_to_the_working_directory() -> None:
    assert resolve_root(None) == Path.cwd().resolve()


def test_root_is_made_absolute(tmp_path: Path) -> None:
    """A relative --root under systemd would reintroduce the cwd dependency."""
    nested = tmp_path / "checkout" / ".." / "checkout"
    (tmp_path / "checkout").mkdir()
    resolved = resolve_root(nested)
    assert resolved.is_absolute()
    assert ".." not in resolved.parts


def test_relative_paths_resolve_under_the_root(tmp_path: Path) -> None:
    assert resolve_under_root(Path("data/x.db"), tmp_path) == tmp_path / "data/x.db"


def test_absolute_paths_ignore_the_root(tmp_path: Path) -> None:
    absolute = (tmp_path / "elsewhere.db").resolve()
    assert resolve_under_root(absolute, Path("/other")) == absolute


def test_tracer_refuses_a_root_that_does_not_exist(tmp_path: Path) -> None:
    """Caught before the config is even read: a wrong root invalidates everything.

    Under systemd a typo in WorkingDirectory used to mean the KILL file was
    looked for in a directory nobody would ever write to, in silence.
    """
    missing = tmp_path / "not-a-checkout"
    result = runner.invoke(
        app,
        ["tracer", "--root", str(missing), "-c", "config.yaml.example", "-d", "1s"],
    )
    assert result.exit_code == 7
    assert "racine introuvable" in result.stdout
