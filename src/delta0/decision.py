"""Pure decision engine — no I/O, ever.

This module receives a `Snapshot`, applies the priority table (README section 7),
and returns an `Action`. It also owns the target-state solver used by BUILD /
RECENTER / SKIM (README section 3).

Every function here MUST be pure. If you need a clock, pass it in.
If you need random, pass it in. If you need I/O — you are in the wrong file.
"""

from __future__ import annotations

from delta0.config import Config
from delta0.types import TargetState


def target_state(equity: float, config: Config) -> TargetState:
    """Solve for the target state given current equity.

    README section 3:
        spot_target     = equity * exposure_mult
        notional_target = spot_target
        margin_target   = spot_target * target_margin_ratio
        debt_target     = target_ltv * collateral_target

    where collateral_target ~= spot_target + cushion (we treat the cushion as
    a separate reserve, so the LTV target applies to the wstETH leg).
    """
    if equity <= 0.0:
        raise ValueError(f"equity must be positive, got {equity}")

    spot_target = equity * config.exposure_mult
    notional_target = spot_target
    margin_target = spot_target * config.target_margin_ratio
    debt_target = config.target_ltv * spot_target

    return TargetState(
        spot_target_usd=spot_target,
        notional_target_usd=notional_target,
        margin_target_usd=margin_target,
        debt_target_usd=debt_target,
    )
