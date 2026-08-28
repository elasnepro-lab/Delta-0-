"""TracerLoop micro-op scheduler — fires executors on config intervals."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from delta0.config import Config
from delta0.safety import SafetyRefused
from delta0.state import StateStore
from delta0.tracer import TracerLoop
from delta0.types import Snapshot
from delta0.watchdog import Watchdog


def _stable_snap() -> Snapshot:
    return Snapshot(
        ts=datetime.now(UTC),
        wsteth_atoken_balance=20.0,
        wsteth_price_usd=2_500.0,
        usdc_atoken_balance=1_000.0,
        usdc_variable_debt_balance=35_000.0,
        hf=1.5,
        aave_lt_wsteth=0.83,
        aave_ltv_max_wsteth=0.80,
        aave_emode=0,
        mark_price=2_500.0,
        short_size_eth=20.0,
        isolated_margin_usd=5_000.0,
        hl_maintenance_margin=0.02,
        funding_last_hour=1.25e-5,
        funding_30d_annualized=0.11,
        borrow_apr=0.05,
        gas_eth=0.01,
        ws_last_tick_age_s=1.0,
        rpc_ok=True,
    )


@dataclass(slots=True)
class _FakeWatcher:
    async def snapshot(self) -> Snapshot:
        return _stable_snap()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    # Anchor set so P7 doesn't fire; stable snap avoids all triggers.
    await s.kv_set("anchor_price", "2500.0")
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_scheduler_fires_aave_cycle_when_interval_zero(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    aave = AsyncMock()
    cfg = config.model_copy(
        update={"tracer": config.tracer.model_copy(update={"aave_cycle_every_s": 1})},
    )
    wd = Watchdog(config=cfg.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=_FakeWatcher(),
        watchdog=wd,
        store=store,
        config=cfg,
        cadence_s=0.0,
        aave_executor=aave,
    )
    # A tiny duration is enough — the loop iterates fast on cadence 0.
    await loop.run(duration_s=0.05)
    # Full cycle: approve, supply, borrow, approve buffer, repay_all, withdraw.
    aave.approve.assert_called()
    aave.supply.assert_called()
    aave.borrow.assert_called()
    aave.repay_all.assert_called()
    aave.withdraw.assert_called()


@pytest.mark.asyncio
async def test_scheduler_does_not_fire_before_interval(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    aave = AsyncMock()
    # 1 hour interval — should never fire within a 50ms test.
    cfg = config.model_copy(
        update={"tracer": config.tracer.model_copy(update={"aave_cycle_every_s": 3600})},
    )
    wd = Watchdog(config=cfg.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=_FakeWatcher(),
        watchdog=wd,
        store=store,
        config=cfg,
        cadence_s=0.0,
        aave_executor=aave,
    )
    # Bump the "last fired" so the initial 0.0 doesn't immediately trigger.
    loop._last_aave_cycle = 999999.0  # any large value > monotonic now
    await loop.run(duration_s=0.02)
    aave.approve.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_hl_and_bridge_fire_independently(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    hl = AsyncMock()
    bridge = AsyncMock()
    cfg = config.model_copy(
        update={
            "tracer": config.tracer.model_copy(
                update={
                    "hl_cancel_every_s": 1,
                    "bridge_every_s": 1,
                    "aave_cycle_every_s": 3600,  # not called in this test
                },
            ),
        },
    )
    wd = Watchdog(config=cfg.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=_FakeWatcher(),
        watchdog=wd,
        store=store,
        config=cfg,
        cadence_s=0.0,
        hl_executor=hl,
        bridge_executor=bridge,
    )
    await loop.run(duration_s=0.05)
    hl.post_and_cancel.assert_called()
    bridge.round_trip.assert_called()


@pytest.mark.asyncio
async def test_scheduler_survives_safety_refused(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    aave = AsyncMock()
    aave.approve.side_effect = SafetyRefused("test refusal")
    cfg = config.model_copy(
        update={"tracer": config.tracer.model_copy(update={"aave_cycle_every_s": 1})},
    )
    wd = Watchdog(config=cfg.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=_FakeWatcher(),
        watchdog=wd,
        store=store,
        config=cfg,
        cadence_s=0.0,
        aave_executor=aave,
    )
    # Loop must not raise; refused ops are best-effort.
    await loop.run(duration_s=0.02)
    aave.approve.assert_called()


@pytest.mark.asyncio
async def test_scheduler_no_ops_when_executors_none(
    config: Config,
    store: StateStore,
    tmp_path: Path,
) -> None:
    # Baseline: no executors provided -> no micro-ops fired, only shadow flow.
    wd = Watchdog(config=config.watchdog, project_root=tmp_path)
    loop = TracerLoop(
        watcher=_FakeWatcher(),
        watchdog=wd,
        store=store,
        config=config,
        cadence_s=0.0,
    )
    n = await loop.run(duration_s=0.02)
    assert n == 0  # stable snap -> NOOP -> no shadow intents
