"""Why an operation failed, in a form the journal can keep.

The M1 marche à blanc left 128 failed intents carrying the same generic reason
and an empty transaction field. The cause had to be reconstituted by matching
timestamps across two days, and the diagnosis that mattered — a missing gas
margin, not an ABI or a logic error — was only reachable by re-reading the
findings file. This module makes the journal answer the question instead.

Three shapes of failure, deliberately distinguished:

- `send_error`  : the send or the receipt wait raised. No transaction is known
                  to have reached the chain, so nothing was paid for.
- `revert`      : a transaction was mined with status 0, and replaying it as an
                  `eth_call` at its own block reproduced the revert. The
                  selector names the cause.
- `revert_gas`  : a transaction was mined with status 0 and the replay came back
                  CLEAN. That combination is the signature of gas exhaustion on
                  Arbitrum, and it is the exact case of `aave_findings.md` §7 —
                  where two diagnostic traps live. An `eth_call` is not
                  constrained by the transaction's gas limit, so a green replay
                  does not exonerate gas; and Arbitrum bills the L1 data share
                  separately, so `gasUsed < gasLimit` does not exclude
                  exhaustion either. Naming this case is the whole point:
                  nothing else in the record tells these two apart.

Aave v3 uses custom errors, so web3 surfaces four bytes and nothing else.
`AAVE_ERROR_SELECTORS` holds the ones met so far; an unknown selector is kept
verbatim, because a selector we cannot name is still the thing to go and hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

# Met in the field, with the operation that produced them. Source:
# `memory/aave_findings.md` §8. To decode a new one, keccak the candidate
# signatures and compare the first four bytes.
AAVE_ERROR_SELECTORS: Final[dict[str, str]] = {
    "0x47bc4b2c": "NotEnoughAvailableUserBalance",
    "0x6679996d": "HealthFactorLowerThanLiquidationThreshold",
}

# Plain ERC-20 revert strings, met on the unwind path. They are `require`
# messages rather than custom errors, so they arrive already readable.
_KNOWN_SUBSTRINGS: Final[dict[str, str]] = {
    "transfer amount exceeds balance": "ERC20InsufficientBalance",
    "transfer amount exceeds allowance": "ERC20InsufficientAllowance",
}

_SELECTOR_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{8}")

# A journal column is not a log sink: keep entries short enough to read in a
# table and long enough to name the cause.
_MAX_DETAIL = 400


@dataclass(frozen=True, slots=True)
class FailureCause:
    """One failure, reduced to what a post-mortem actually needs."""

    kind: str  # send_error | revert | revert_gas
    name: str | None = None  # decoded error name, when we can name it
    selector: str | None = None  # raw 4-byte selector, when there is one
    detail: str | None = None  # exception type and message, truncated
    tx_hash: str | None = None  # present whenever a transaction was mined

    def journal_entry(self) -> str:
        """One line, stable field order, safe to read back by eye or by grep."""
        parts = [self.kind]
        if self.name:
            parts.append(self.name)
        if self.selector:
            parts.append(self.selector)
        if self.tx_hash:
            parts.append(f"tx={self.tx_hash}")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


def _truncate(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _MAX_DETAIL else flat[: _MAX_DETAIL - 1] + "…"


def decode_selector(text: str) -> tuple[str | None, str | None]:
    """Pull a custom-error selector out of a web3 error message.

    Returns `(selector, name)`, either of which may be None. An unknown
    selector is returned without a name rather than dropped: it is the value
    to hash candidate signatures against.
    """
    for needle, name in _KNOWN_SUBSTRINGS.items():
        if needle in text:
            return None, name
    match = _SELECTOR_RE.search(text)
    if match is None:
        return None, None
    selector = match.group(0).lower()
    return selector, AAVE_ERROR_SELECTORS.get(selector)


def from_exception(exc: BaseException, *, tx_hash: str | None = None) -> FailureCause:
    """Cause of a failure that raised before any receipt was read."""
    text = f"{type(exc).__name__}: {exc}"
    selector, name = decode_selector(text)
    return FailureCause(
        kind="send_error",
        name=name,
        selector=selector,
        detail=_truncate(text),
        tx_hash=tx_hash,
    )


def from_revert(
    *,
    tx_hash: str,
    replay_error: BaseException | None,
    gas_used: int | None = None,
    gas_limit: int | None = None,
) -> FailureCause:
    """Cause of a transaction mined with status 0.

    `replay_error` is what re-running the call as an `eth_call` raised, or None
    when the replay came back clean — which points at gas rather than at the
    call itself. See this module's docstring for why that distinction cannot be
    read off the receipt on an L2.
    """
    gas = None
    if gas_used is not None and gas_limit is not None:
        gas = f"gas={gas_used}/{gas_limit}"

    if replay_error is None:
        detail = "rejeu eth_call propre — signature d'un manque de gaz (aave_findings §7)"
        return FailureCause(
            kind="revert_gas",
            detail=_truncate(f"{detail} {gas}" if gas else detail),
            tx_hash=tx_hash,
        )

    text = f"{type(replay_error).__name__}: {replay_error}"
    selector, name = decode_selector(text)
    return FailureCause(
        kind="revert",
        name=name,
        selector=selector,
        detail=_truncate(f"{text} {gas}" if gas else text),
        tx_hash=tx_hash,
    )


async def diagnose_revert(
    *,
    call: Any,
    receipt: Any,
    tx_hash: str,
    sender: str,
    gas_used: int,
    gas_limit: int,
) -> FailureCause:
    """Replay a reverted call at its own block to name the cause.

    The receipt of a reverted transaction carries no reason, so the reason has
    to be asked for. Replaying at the mined block reproduces the state the
    transaction saw rather than the state of the chain tip, which may have
    moved since.

    A replay that comes back clean is not an absence of cause: it is the gas
    signature, and `from_revert` names it as such. Every error here is
    classified rather than propagated — a diagnostic that raises would replace
    the failure we are trying to explain with one of its own.
    """
    block = receipt.get("blockNumber")
    try:
        await call.call({"from": sender}, block_identifier=block)
    except Exception as replay_error:
        return from_revert(
            tx_hash=tx_hash,
            replay_error=replay_error,
            gas_used=gas_used,
            gas_limit=gas_limit,
        )
    return from_revert(
        tx_hash=tx_hash,
        replay_error=None,
        gas_used=gas_used,
        gas_limit=gas_limit,
    )
