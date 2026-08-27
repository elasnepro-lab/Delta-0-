"""Swap aggregator wrapper. M0: placeholder — filled in M2/M3."""

from __future__ import annotations


class SwapAggregator:
    """Placeholder for Odos / 1inch swap wrapper.

    See README section 9.4 for the contract: quote-first, strict slippage,
    abandon-and-requote on quote drift > slippage_max_bps.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "Swap aggregator will be implemented in M2/M3. See README section 9.4.",
        )
