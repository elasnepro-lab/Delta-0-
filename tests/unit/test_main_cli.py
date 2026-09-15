"""CLI smoke tests — argument parsing and duration parsing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from delta0.latency import M1_REPORT_STAMP_KEY, M1_REPORT_STATUS_KEY
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


def test_report_refuses_a_database_with_no_measurement(tmp_path: Path) -> None:
    """The runbook trap, closed.

    `delta0 report` without `--db` read `data/delta0.db`, a journal abandoned
    days before the campaign, and rendered a full verdict about the wrong
    database. Naming the path and refusing beats describing nothing: a report
    on an empty base is not a report.
    """
    empty = tmp_path / "empty.db"
    result = runner.invoke(app, ["report", "--db", str(empty), "-c", "config.yaml.example"])
    assert result.exit_code == 2
    assert "REFUS" in result.stdout
    assert "empty.db" in result.stdout
    # And no verdict at all, rather than a comforting one.
    assert "Chemins critiques" not in result.stdout


def test_report_renders_a_measured_path_within_budget(tmp_path: Path) -> None:
    db = tmp_path / "seeded.db"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0, 420.0, 510.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    # Exit 1: P1/P2 holds its budget but the other four paths have no samples,
    # so the M1 criterion is not met. The code is the verdict, machine-readable.
    assert result.exit_code == 1
    assert "OK" in result.stdout
    assert "p1_p2_hl_order" in result.stdout


def test_report_flags_prudent_mode_when_p95_blows_the_budget(tmp_path: Path) -> None:
    db = tmp_path / "slow.db"
    # P1/P2 budget is 2 s; 5 s is past 2 s x 1.5.
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [5_000.0, 5_200.0, 5_400.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert result.exit_code == 1
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


async def _seed_failures(db: Path, rows: list[tuple[str, str, str | None]]) -> None:
    store = StateStore(db)
    await store.open()
    try:
        for i, (action, created_at, cause) in enumerate(rows):
            async with store.transaction() as conn:
                await conn.execute(
                    """INSERT INTO intents
                       (id, created_at, action, priority, params_json, reason,
                        status, updated_at)
                       VALUES (?, ?, ?, 3, '{}', 'micro-op M1-B2', 'failed', ?)""",
                    (f"i{i}", created_at, action, created_at),
                )
            if cause is not None:
                await store.record_intent_failure(f"i{i}", cause)
    finally:
        await store.close()


def test_report_says_so_when_nothing_failed(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert "Aucune intention en échec" in result.stdout


def test_report_groups_failures_by_cause(tmp_path: Path) -> None:
    """The repeating cause must not bury the distinct ones.

    Shape taken from the real campaign: one defect repeating every cycle plus
    a couple of isolated failures. Ordered by count, the repeat comes first and
    its last occurrence dates the end of the outage.
    """
    db = tmp_path / "failed.db"
    asyncio.run(
        _seed_failures(
            db,
            [
                ("aave_supply", "2026-09-10T08:00:00+00:00", "revert_gas | tx=0x1"),
                ("aave_supply", "2026-09-10T08:30:00+00:00", "revert_gas | tx=0x2"),
                ("aave_supply", "2026-09-11T08:14:59+00:00", "revert_gas | tx=0x3"),
                ("hl_post_only_cancel", "2026-09-05T08:33:37+00:00", None),
            ],
        ),
    )
    # A latency sample so the report renders at all: a journal with failures
    # but no measurement is still an empty report, and refused as such.
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert "total: 4" in result.stdout
    assert "revert_gas" in result.stdout
    # An unrecorded cause is named as such, never rendered as a blank.
    assert "non enregistrée" in result.stdout
    # Grouped, so the three identical ones are one row carrying a count.
    assert "aave_supply" in result.stdout


# --- Un rapport qui ne peut pas mentir (chantier 4.9) ------------------------


async def _seed_at(db: Path, path_name: str, when: str, value: float) -> None:
    """Insert one latency sample with a chosen timestamp."""
    store = StateStore(db)
    await store.open()
    try:
        async with store.transaction() as conn:
            await conn.execute(
                "INSERT INTO latencies (ts, path, duration_ms) VALUES (?, ?, ?)",
                (when, path_name, value),
            )
    finally:
        await store.close()


def test_the_window_excludes_an_older_campaign(tmp_path: Path) -> None:
    """18,3 % of `m1_run.db` predated the campaign it was meant to describe.

    Every maximum in the M1 report came from that older session. Without a
    window the report describes a campaign that is not its own.
    """
    db = tmp_path / "two_campaigns.db"
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # Enough old samples that they own the p95, which is the whole point:
    # a handful of stale outliers decided the verdict of the M1 report.
    for _ in range(5):
        asyncio.run(_seed_at(db, "path.p1_p2_hl_order", old, 9_000.0))
    asyncio.run(_seed_at(db, "path.p1_p2_hl_order", recent, 300.0))

    whole = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    windowed = runner.invoke(
        app,
        ["report", "--db", str(db), "-c", "config.yaml.example", "--days", "7"],
    )
    # The old 9 s sample blows the 2 s budget; the window leaves only the 300 ms.
    assert "PRUDENT" in whole.stdout
    assert "PRUDENT" not in windowed.stdout
    assert "7 derniers jours" in windowed.stdout


def test_the_window_can_empty_the_report_and_says_so(tmp_path: Path) -> None:
    db = tmp_path / "stale.db"
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    asyncio.run(_seed_at(db, "path.p1_p2_hl_order", old, 300.0))
    result = runner.invoke(
        app,
        ["report", "--db", str(db), "-c", "config.yaml.example", "--days", "7"],
    )
    assert result.exit_code == 2
    assert "7 derniers jours" in result.stdout


def test_the_report_always_names_its_journal_and_window(tmp_path: Path) -> None:
    """A number without its provenance is how the wrong base went unnoticed."""
    db = tmp_path / "named.db"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0]}))
    result = runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])
    assert "named.db" in result.stdout
    assert "tout le journal" in result.stdout


def test_the_json_artifact_carries_the_verdict(tmp_path: Path) -> None:
    db = tmp_path / "artifact.db"
    out = tmp_path / "nested" / "report.json"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0, 420.0]}))
    result = runner.invoke(
        app,
        ["report", "--db", str(db), "-c", "config.yaml.example", "--json", str(out)],
    )
    assert result.exit_code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["m1_speed_criterion_met"] is False
    assert payload["database"].endswith("artifact.db")
    p1 = next(p for p in payload["critical_paths"] if p["key"] == "P1/P2")
    assert p1["samples"] == 2
    assert p1["budget_ms"] == 2_000.0
    # P4 declares the leg M1 cannot measure, so a reader knows what was excused.
    p4 = next(p for p in payload["critical_paths"] if p["key"] == "P4")
    assert p4["unmeasured_legs"]


def test_the_report_stamps_the_journal_it_read(tmp_path: Path) -> None:
    """The evidence travels with the journal, not with the machine.

    A report about another campaign must not unlock this one, so the stamp
    goes into the base that was read.
    """
    db = tmp_path / "stamped.db"
    asyncio.run(_seed(db, {"path.p1_p2_hl_order": [300.0]}))
    runner.invoke(app, ["report", "--db", str(db), "-c", "config.yaml.example"])

    async def _read() -> tuple[str | None, str | None]:
        store = StateStore(db)
        await store.open()
        try:
            return (
                await store.kv_get(M1_REPORT_STAMP_KEY),
                await store.kv_get(M1_REPORT_STATUS_KEY),
            )
        finally:
            await store.close()

    stamp, status = asyncio.run(_read())
    assert stamp is not None
    assert status == "ECHEC"  # only one of the five paths has samples
