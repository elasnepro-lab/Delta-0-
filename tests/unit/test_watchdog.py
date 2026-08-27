"""Watchdog transitions, KILL files, ws staleness."""

from __future__ import annotations

from pathlib import Path

from delta0.config import Config
from delta0.decision import BlindState
from delta0.watchdog import KillSignal, Watchdog


def _make(config: Config, tmp_path: Path) -> Watchdog:
    return Watchdog(config=config.watchdog, project_root=tmp_path)


def test_starts_nominal(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    # No `now` override: query with the real monotonic clock so the elapsed
    # time since construction is essentially zero. Passing a hardcoded `now`
    # here is fragile (`time.monotonic()` at boot differs by orders of
    # magnitude across OSes and CI runners).
    assert wd.blind_state() == BlindState.NOMINAL


def test_ws_staleness_marks_hl_blind(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    wd.mark_ws_tick(now=0.0)
    wd.mark_hl_ok(now=0.0)
    wd.mark_aave_ok(now=0.0)
    # WS budget is ws_stale_s = 10 s.
    assert wd.blind_state(now=5.0) == BlindState.NOMINAL
    assert wd.blind_state(now=11.0) == BlindState.AAVE_ONLY


def test_aave_failures_reach_blind_after_tx_fail_max(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    wd.mark_ws_tick(now=0.0)
    wd.mark_hl_ok(now=0.0)
    for _ in range(config.watchdog.tx_fail_max):
        wd.mark_aave_failure()
    assert wd.blind_state(now=0.5) == BlindState.HL_ONLY


def test_both_venues_blind_when_ws_stale_and_aave_failed(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    wd.mark_ws_tick(now=0.0)
    wd.mark_hl_ok(now=0.0)
    for _ in range(config.watchdog.tx_fail_max):
        wd.mark_aave_failure()
    assert wd.blind_state(now=100.0) == BlindState.BOTH_BLIND


def test_recovery_after_ok(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    for _ in range(config.watchdog.tx_fail_max):
        wd.mark_aave_failure()
    assert wd.blind_state(now=0.5) in (BlindState.HL_ONLY, BlindState.BOTH_BLIND)
    wd.mark_aave_ok(now=1.0)
    wd.mark_ws_tick(now=1.0)
    wd.mark_hl_ok(now=1.0)
    assert wd.blind_state(now=1.0) == BlindState.NOMINAL


def test_kill_signal_none(config: Config, tmp_path: Path) -> None:
    wd = _make(config, tmp_path)
    assert wd.kill_signal() == KillSignal.NONE


def test_kill_signal_pause(config: Config, tmp_path: Path) -> None:
    (tmp_path / "KILL").write_text("")
    wd = _make(config, tmp_path)
    assert wd.kill_signal() == KillSignal.PAUSE


def test_kill_signal_deflate_wins_over_pause(config: Config, tmp_path: Path) -> None:
    (tmp_path / "KILL").write_text("")
    (tmp_path / "KILL_DEFLATE").write_text("")
    wd = _make(config, tmp_path)
    assert wd.kill_signal() == KillSignal.DEFLATE
