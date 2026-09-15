"""The micro-op executors as the CLI actually builds them.

The balance check written after the 8 September incident had its tests, and
none of them could see that production never handed the executor its reader:
they all built the executor by hand (audit dev 2026-09-16, 1.1). This test goes
through the function the tracer calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from delta0.config import Config
from delta0.main import _wire_micro_op_executors
from delta0.settings import Settings
from delta0.state import StateStore


def test_the_aave_executor_is_wired_with_the_balance_reader(
    config: Config,
    tmp_path: Path,
) -> None:
    balances = object()
    settings = Settings(
        arbitrum_rpc_primary="http://127.0.0.1:1",
        bot_master_address="0x000000000000000000000000000000000000dEaD",
    )
    # A rehearsal loads no key; the SDK read client would fetch metadata over HTTP.
    with patch("delta0.main.make_info", return_value=MagicMock()):
        aave, _hl, _bridge = _wire_micro_op_executors(
            cfg=config,
            settings=settings,
            store=StateStore(tmp_path / "state.db"),
            w3=MagicMock(),
            confirmed_kinds=[],
            rehearse=True,
            project_root=tmp_path,
            balances=balances,  # type: ignore[arg-type]
        )

    assert aave._balances is balances
