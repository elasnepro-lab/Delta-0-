"""Gas-limit margin for outgoing transactions.

`web3.contract.build_transaction` fills the `gas` field with the raw result of
`eth_estimateGas` and applies no margin. On Arbitrum that estimate is not a
stable quantity: it bundles the L2 execution cost with an L1 data-posting cost
derived from the current L1 base fee, and that second term moves between the
moment the estimate is taken and the moment the transaction is included. When
it moves up, the transaction runs out of gas and reverts — having paid for the
attempt and changed nothing.

Observed on 2026-09-02, first live Aave cycle: the `repay` was sent with a
168 594 limit and reverted; the same call needs ~169 615 on an anvil fork of the
same chain. A 0.6 % shortfall was enough to lose the transaction, and because
the repay failed the following `withdraw` failed too — leaving an open position.

Unused gas is refunded, so the only cost of a wide margin is a slightly higher
balance requirement while the transaction is in flight. For a bot that must run
seven days unattended, that trade is not close.
"""

from __future__ import annotations

# 35 %: comfortably above the L1-component swings seen on Arbitrum, while still
# small enough that a runaway estimate cannot drain the operational float.
GAS_LIMIT_MARGIN = 1.35


def with_gas_margin(estimated_gas: int) -> int:
    """Widen an `eth_estimateGas` result into a gas limit safe to send with."""
    return int(estimated_gas * GAS_LIMIT_MARGIN)
