"""MicroOpsGuard — allowlist, caps, rate limit, KILL, first-use gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from delta0.config import TracerConfig
from delta0.safety import ALLOWED_OP_KINDS, MicroOpsGuard, SafetyRefused


def _guard(
    tmp_path: Path,
    *,
    dry_run: bool = True,
    require_confirm: bool = True,
    max_op_usd: float = 15.0,
    max_ops_per_hour: int = 20,
) -> MicroOpsGuard:
    cfg = TracerConfig(
        max_op_usd=max_op_usd,
        max_ops_per_hour=max_ops_per_hour,
        require_first_use_confirmation=require_confirm,
        dry_run=dry_run,
    )
    return MicroOpsGuard(config=cfg, project_root=tmp_path)


def test_unknown_op_refused(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    with pytest.raises(SafetyRefused, match="non autorisée"):
        g.check("weird_op", notional_usd=5.0, now=0.0)


def test_confirm_kind_unknown_raises(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    with pytest.raises(ValueError, match="unknown op kind"):
        g.confirm_kind("nonsense")


def test_first_use_blocks_without_confirm(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    with pytest.raises(SafetyRefused, match="confirmation"):
        g.check("aave_supply", notional_usd=5.0, now=0.0)


def test_confirm_kind_unblocks(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    g.confirm_kind("aave_supply")
    g.check("aave_supply", notional_usd=5.0, now=0.0)  # must not raise


def test_disable_confirmation(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False)
    g.check("aave_supply", notional_usd=5.0, now=0.0)


def test_cap_refuses_above_max(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False, max_op_usd=10.0)
    with pytest.raises(SafetyRefused, match="plafond"):
        g.check("aave_supply", notional_usd=10.01, now=0.0)


def test_negative_notional_refused(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False)
    with pytest.raises(SafetyRefused, match="négatif"):
        g.check("aave_supply", notional_usd=-1.0, now=0.0)


def test_kill_file_blocks(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False)
    (tmp_path / "KILL").write_text("")
    with pytest.raises(SafetyRefused, match="KILL"):
        g.check("aave_supply", notional_usd=5.0, now=0.0)


def test_kill_deflate_also_blocks(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False)
    (tmp_path / "KILL_DEFLATE").write_text("")
    with pytest.raises(SafetyRefused, match="KILL"):
        g.check("aave_supply", notional_usd=5.0, now=0.0)


def test_rate_limit_refuses_beyond_hour_budget(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False, max_ops_per_hour=3)
    for i in range(3):
        g.check("aave_supply", notional_usd=1.0, now=float(i))
    with pytest.raises(SafetyRefused, match="fréquence"):
        g.check("aave_supply", notional_usd=1.0, now=3.5)


def test_rate_limit_forgets_old_timestamps(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False, max_ops_per_hour=2)
    g.check("aave_supply", notional_usd=1.0, now=0.0)
    g.check("aave_supply", notional_usd=1.0, now=100.0)
    # 4000 s later, old timestamps age out.
    g.check("aave_supply", notional_usd=1.0, now=4000.0)  # must not raise


def test_ops_in_last_hour_counter(tmp_path: Path) -> None:
    g = _guard(tmp_path, require_confirm=False)
    for i in range(5):
        g.check("aave_supply", notional_usd=1.0, now=float(i * 10))
    assert g.ops_in_last_hour(now=100.0) == 5
    assert g.ops_in_last_hour(now=4000.0) == 0


def test_allowlist_covers_all_paths(tmp_path: Path) -> None:
    # If a new op kind is added, the tests must be extended too.
    expected = {
        "aave_approve",
        "aave_supply",
        "aave_borrow",
        "aave_repay",
        "aave_withdraw",
        "hl_post_only_cancel",
        "bridge_out",
        "bridge_in",
    }
    assert expected == ALLOWED_OP_KINDS
