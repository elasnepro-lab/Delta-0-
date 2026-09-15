"""Failure classification — selectors, the gas signature, journal entries."""

from __future__ import annotations

from typing import Any

import pytest
from web3.exceptions import ContractLogicError

from delta0 import failure


def test_known_aave_selector_is_named() -> None:
    selector, name = failure.decode_selector("execution reverted: 0x6679996d")
    assert selector == "0x6679996d"
    assert name == "HealthFactorLowerThanLiquidationThreshold"


def test_unknown_selector_is_kept_without_a_name() -> None:
    """An unnamed selector is still the value to hash candidates against."""
    selector, name = failure.decode_selector("execution reverted: 0xdeadbeef")
    assert selector == "0xdeadbeef"
    assert name is None


def test_selector_case_is_normalised() -> None:
    selector, name = failure.decode_selector("reverted 0x6679996D")
    assert selector == "0x6679996d"
    assert name == "HealthFactorLowerThanLiquidationThreshold"


def test_erc20_require_strings_are_named_without_a_selector() -> None:
    """The unwind path meets `require` messages, not custom errors."""
    selector, name = failure.decode_selector(
        "ContractLogicError: execution reverted: ERC20: transfer amount exceeds balance",
    )
    assert selector is None
    assert name == "ERC20InsufficientBalance"


def test_text_with_no_cause_yields_nothing() -> None:
    assert failure.decode_selector("timed out") == (None, None)


def test_send_error_keeps_the_exception_type_and_message() -> None:
    cause = failure.from_exception(TimeoutError("no receipt in 120s"))
    assert cause.kind == "send_error"
    assert cause.tx_hash is None
    assert "TimeoutError" in cause.journal_entry()
    assert "no receipt in 120s" in cause.journal_entry()


def test_a_clean_replay_is_reported_as_the_gas_signature() -> None:
    """aave_findings §7: status 0 plus a green replay means gas, not logic.

    This is the distinction the M1 incident needed and could not read
    anywhere: neither the receipt nor a replay alone tells the two apart.
    """
    cause = failure.from_revert(
        tx_hash="0xabc",
        replay_error=None,
        gas_used=163_761,
        gas_limit=168_594,
    )
    assert cause.kind == "revert_gas"
    assert cause.selector is None
    entry = cause.journal_entry()
    assert "gaz" in entry
    assert "gas=163761/168594" in entry


def test_a_reproduced_revert_names_the_selector() -> None:
    cause = failure.from_revert(
        tx_hash="0xabc",
        replay_error=ValueError("execution reverted: 0x47bc4b2c"),
        gas_used=10,
        gas_limit=20,
    )
    assert cause.kind == "revert"
    assert cause.name == "NotEnoughAvailableUserBalance"
    assert cause.selector == "0x47bc4b2c"


def test_journal_entry_field_order_is_stable() -> None:
    """The column is read by eye and by grep, so the order must not drift."""
    cause = failure.FailureCause(
        kind="revert",
        name="Boom",
        selector="0xdeadbeef",
        detail="ValueError: boom",
        tx_hash="0xabc",
    )
    assert cause.journal_entry() == "revert | Boom | 0xdeadbeef | tx=0xabc | ValueError: boom"


def test_long_details_are_truncated_and_flattened() -> None:
    cause = failure.from_exception(ValueError("x" * 900 + "\n\nspread   out"))
    entry = cause.journal_entry()
    assert len(entry) < 500
    assert "\n" not in entry


class _FakeCall:
    """Stands in for a web3 contract call, recording how it was replayed."""

    def __init__(self, error: Exception | None) -> None:
        self._error = error
        self.seen: dict[str, Any] = {}

    async def call(self, tx: dict[str, Any], block_identifier: Any = None) -> int:
        self.seen = {"tx": tx, "block": block_identifier}
        if self._error is not None:
            raise self._error
        return 0


@pytest.mark.asyncio
async def test_replay_runs_at_the_mined_block_not_the_chain_tip() -> None:
    """The tip may have moved; only the mined block holds the state seen."""
    # What web3 7 raises when the replayed call reverts with Aave's custom error.
    call = _FakeCall(ContractLogicError("execution reverted: 0x6679996d", data="0x6679996d"))
    cause = await failure.diagnose_revert(
        call=call,
        receipt={"blockNumber": 504_446_045},
        tx_hash="0xabc",
        sender="0xSender",
        gas_used=1,
        gas_limit=2,
    )
    assert call.seen["block"] == 504_446_045
    assert call.seen["tx"] == {"from": "0xSender"}
    assert cause.name == "HealthFactorLowerThanLiquidationThreshold"


@pytest.mark.asyncio
async def test_a_bug_in_the_replay_is_not_passed_off_as_the_revert_reason() -> None:
    """Chantier 6.4: classified, an AttributeError would pose as the revert reason."""
    with pytest.raises(AttributeError, match="boom"):
        await failure.diagnose_revert(
            call=_FakeCall(AttributeError("boom")),
            receipt={"blockNumber": 1},
            tx_hash="0xabc",
            sender="0xSender",
            gas_used=1,
            gas_limit=2,
        )


@pytest.mark.asyncio
async def test_replay_without_revert_lands_on_the_gas_verdict() -> None:
    cause = await failure.diagnose_revert(
        call=_FakeCall(None),
        receipt={"blockNumber": 1},
        tx_hash="0xabc",
        sender="0xSender",
        gas_used=1,
        gas_limit=2,
    )
    assert cause.kind == "revert_gas"


@pytest.mark.asyncio
async def test_a_replay_that_cannot_run_still_yields_a_cause() -> None:
    """A diagnostic must never replace the failure it is explaining.

    The RPC can refuse an archival call at an old block. That refusal is
    itself classified rather than propagated, so the caller keeps its own
    error path intact.
    """
    cause = await failure.diagnose_revert(
        call=_FakeCall(ConnectionError("missing trie node")),
        receipt={"blockNumber": 1},
        tx_hash="0xabc",
        sender="0xSender",
        gas_used=1,
        gas_limit=2,
    )
    assert cause.kind == "revert"
    assert cause.tx_hash == "0xabc"
    assert "missing trie node" in cause.journal_entry()
