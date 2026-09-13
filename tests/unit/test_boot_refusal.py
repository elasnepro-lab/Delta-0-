"""A boot the reconciliation refuses ends in a readable refusal, not a traceback.

Found on 2026-09-14 while checking a code comment: `delta0 tracer` would not
start on the operator's account, emptied since the M1 position was closed. Aave
reports a liquidation threshold of 0 for an account without collateral, the
bands check refused it — rightly — but through a bare ValueError the CLI never
caught. The 48-hour read-only probe planned on the server would have died the
same way. The reserve threshold now covers the empty account (see
test_aave_multicall.py); these tests pin the refusal that remains for a
threshold that is genuinely unreadable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import pytest

from delta0.config import Config
from delta0.errors import BootRefused, Delta0Error
from delta0.main import _reconcile_boot
from delta0.reconcile import reconcile_at_boot
from delta0.state import StateStore
from delta0.types import Snapshot
from tests.world import reference_snapshot


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[StateStore, None]:
    s = StateStore(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@dataclass
class _Watcher:
    config: Config
    snap: Snapshot

    async def snapshot(self) -> Snapshot:
        return self.snap


@pytest.mark.asyncio
async def test_an_unreadable_threshold_raises_a_boot_refusal(
    config: Config, store: StateStore
) -> None:
    with pytest.raises(BootRefused, match="LT"):
        await reconcile_at_boot(store, reference_snapshot(aave_lt_wsteth=0.0), config)
    assert issubclass(BootRefused, Delta0Error)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_the_cli_refuses_the_boot_cleanly_in_every_mode(
    config: Config, store: StateStore, strict: bool
) -> None:
    """Returned, not raised: `tracer` turns False into exit code 5."""
    watcher = _Watcher(config, reference_snapshot(aave_lt_wsteth=0.0))
    assert await _reconcile_boot(store, watcher, strict=strict) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_reference_world_boots(config: Config, store: StateStore) -> None:
    watcher = _Watcher(config, reference_snapshot())
    assert await _reconcile_boot(store, watcher, strict=False) is True  # type: ignore[arg-type]
