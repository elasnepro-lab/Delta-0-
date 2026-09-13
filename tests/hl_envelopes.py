"""Hyperliquid responses captured live, kept verbatim as fixtures (audit D2).

Retyping one of these "to tidy it" is how a test ends up asserting the shape we
imagined instead of the one the venue sends. Sources: memory/hl_findings.md.

This file is a helper, not a test module — outside pytest's `test_*` pattern.
"""

from __future__ import annotations

from typing import Any

# Testnet, 2026-09-08, orders signed by the agent on the funded master account
# (§7, audit C1). The refusal is NESTED inside `status: ok`.
C1_MIN_NOTIONAL: dict[str, Any] = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=4"}]},
    },
}
C1_INSUFFICIENT_MARGIN: dict[str, Any] = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"error": "Insufficient margin to place order. asset=4"}]},
    },
}

# Testnet, unfunded throwaway key: refused at the first level (§7). The address
# was truncated when the finding was written down.
UNKNOWN_ACCOUNT: dict[str, Any] = {
    "status": "err",
    "response": "User or API Wallet 0x1ef3... does not exist.",
}

# Mainnet, 2026-09-02: spot-to-perp transfer on a unified account (§1).
UNIFIED_ACCOUNT_TRANSFER: dict[str, Any] = {
    "status": "err",
    "response": "Action disabled when unified account is active",
}

# Testnet, 2026-09-08: `update_isolated_margin(+5 USDC)` accepted (§11).
MARGIN_UPDATE_ACCEPTED: dict[str, Any] = {"status": "ok", "response": {"type": "default"}}
