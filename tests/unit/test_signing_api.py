"""Pin the eth-account signing API the executors depend on.

`_journal_and_send` (executor.py) and `_send` (venues/bridge.py) both reach for
`signed.raw_transaction`. web3.py v7 renamed that attribute from the v6 spelling
`rawTransaction`, and the rename is invisible to every other test in this suite:
those drive the executors with `MagicMock`, which fabricates whichever attribute
it is asked for and returns a mock. The bug therefore survives a fully green
suite and only surfaces on the first real signed transaction — which, during
M1, means the first micro-op of a live session.

The anvil precheck does not cover it either: `scripts/precheck_aave_fork.py`
sends through `anvil_impersonateAccount`, so it never signs anything.

This test signs a throwaway transaction with a throwaway key — no network, no
config, no secrets — and asserts the attribute the executors actually read.
"""

from __future__ import annotations

from typing import cast

from eth_account import Account
from eth_account.datastructures import SignedTransaction

# Well-known burner key from the eth-account test vectors. Never funded.
_THROWAWAY_KEY = "0x" + "11" * 32


def _sign_dummy() -> SignedTransaction:
    signed = Account.from_key(_THROWAWAY_KEY).sign_transaction(
        {
            "to": "0x0000000000000000000000000000000000000000",
            "value": 0,
            "gas": 21_000,
            "maxFeePerGas": 10**9,
            "maxPriorityFeePerGas": 10**9,
            "nonce": 0,
            "chainId": 42161,
        },
    )
    # `sign_transaction` is untyped upstream; the cast keeps mypy strict here.
    return cast("SignedTransaction", signed)


def test_signed_transaction_exposes_raw_transaction() -> None:
    """The name the executors send. A rename here breaks every live micro-op."""
    signed = _sign_dummy()
    assert hasattr(signed, "raw_transaction")
    assert isinstance(signed.raw_transaction, bytes)


def test_web3_v6_spelling_is_gone() -> None:
    """Guards the reverse mistake: reintroducing the v6 `rawTransaction`.

    If a dependency bump ever restores this alias, the assertion fails and
    forces a deliberate decision instead of a silent divergence between what
    the code reads and what the library offers.
    """
    signed = _sign_dummy()
    assert not hasattr(signed, "rawTransaction")
