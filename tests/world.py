"""The reference world every test starts from — one place, not five copies.

16 wstETH priced 3 125 $ by the Aave oracle (50 000 $ of spot), worth 1.25 ETH
each, so 20 ETH of equivalent against a 20 ETH short: delta zero. Arbitrum's
real wstETH parameters, read on-chain (memory/aave_findings.md §9): liquidation
threshold 0.79 and maximum LTV 0.75, with the health factor they give on
51 000 $ of collateral and 33 750 $ of debt — `target_ltv` 0.675 on the spot
alone, the solver's base. The debt was 35 000 (0.70 on the spot) until the
2026-10-02 finance review: the world sat off target, and only I2 reading
Aave's LTV, cushion included, let it pass for a position at rest (F7, m4).

Four test modules used to copy this sheet by hand with LT 0.83 and HF 1.5 — the
world phase 1 corrected, still alive in the tracer tests — and every copy
carried a maximum LTV of 0.80, above the liquidation threshold, which Aave
never allows (chantier 6.3). A test that needs a different state says so with
`reference_snapshot(field=value)`.

This file is a helper, not a test module.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from delta0.types import Snapshot

# A Monday morning, far from the Sunday skim slot.
REFERENCE_TS = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
LT_ARBITRUM = 0.79
LTV_MAX_ARBITRUM = 0.75

_REFERENCE = Snapshot(
    ts=REFERENCE_TS,
    wsteth_atoken_balance=16.0,
    wsteth_price_usd=3_125.0,
    wsteth_eth_ratio=1.25,
    usdc_atoken_balance=1_000.0,
    usdc_variable_debt_balance=33_750.0,
    usdc_wallet_balance=0.0,
    hf=1.1938,  # 0.79 x 51 000 / 33 750
    aave_lt_wsteth=LT_ARBITRUM,
    aave_ltv_max_wsteth=LTV_MAX_ARBITRUM,
    aave_emode=0,
    mark_price=2_500.0,
    short_size_eth=20.0,
    isolated_margin_usd=5_000.0,
    hl_free_usdc=1_000.0,
    hl_maintenance_margin=0.02,
    funding_last_hour=1.25e-5,
    funding_30d_annualized=0.11,
    borrow_apr=0.05,
    gas_eth=0.01,
    ws_last_tick_age_s=1.0,
    rpc_ok=True,
)


def reference_snapshot(**overrides: Any) -> Snapshot:
    """The reference position at rest, with the fields a test needs changed."""
    return replace(_REFERENCE, **overrides)
