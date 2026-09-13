"""Amounts crossing into a token's native units, with a rounding someone chose.

`int(amount * 10**decimals)` is how the executors turned 5.0 USDC into
5_000_000. It truncates toward zero on a binary float: on 10 000 random USDC
amounts with six decimals, 154 came out one unit short — 1.5 %. The tracer's
round amounts (5.0, 1.0, 2.0) happened to escape it, which is why M1 never
showed it; the amounts phase-8 executors compute will not. At 18 decimals the
same `int()` turns 2.3 wstETH into 2 299 999 999 999 999 744 wei.

`to_raw` makes the rounding a decision of the caller instead of an accident of
the representation. Floats stay where their precision is enough — observations
and decision ratios. Only the boundary where an amount becomes the integer sent
on-chain goes through `Decimal` (chantier 6.5).
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal
from enum import Enum


class Rounding(Enum):
    DOWN = ROUND_FLOOR  # never more than asked: what spends, borrows or withdraws
    UP = ROUND_CEILING  # never less than asked: what authorizes or pays back


# A binary float holds 15 significant digits reliably; the digits past them are
# representation noise (0.1 + 0.2 == 0.30000000000000004). Dropping them first
# keeps an UP rounding from charging a whole unit for noise.
_FLOAT_DIGITS = Context(prec=15)
# Wide enough for any uint256 amount.
_WIDE = Context(prec=100)


def to_raw(amount: float | int | Decimal | str, decimals: int, rounding: Rounding) -> int:
    """`amount` in native units of a token with `decimals`, rounded as stated.

    Floats are denoised to their 15 reliable significant digits before the
    rounding; `Decimal`, `int` and `str` inputs are taken exactly.
    """
    if decimals < 0:
        raise ValueError(f"décimales négatives : {decimals}")
    if isinstance(amount, float):
        if not math.isfinite(amount):
            raise ValueError(f"montant non fini : {amount!r}")
        value = _FLOAT_DIGITS.create_decimal(repr(amount))
    else:
        value = Decimal(amount)
        if not value.is_finite():
            raise ValueError(f"montant non fini : {amount!r}")
    if value < 0:
        raise ValueError(f"montant négatif : {amount!r}")
    scaled = value.scaleb(decimals, context=_WIDE)
    return int(scaled.to_integral_value(rounding=rounding.value, context=_WIDE))
