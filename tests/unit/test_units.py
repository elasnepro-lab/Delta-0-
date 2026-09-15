"""Chantier 6.5 — amounts become native units with a rounding someone chose.

`int(amount * 10**decimals)` truncated toward zero: one six-decimal USDC amount
in 65 left the executor a unit short, and 2.3 wstETH lost 256 wei. The
tracer's round amounts hid it during M1. These tests pin the bug next to its
fix, and keep the old conversion from coming back.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from delta0.executor import _ROUNDING
from delta0.units import Rounding, to_raw

SRC = Path(__file__).resolve().parents[2] / "src" / "delta0"


@pytest.mark.parametrize("amount", [8.12, 33.3, 1.001, 1.005])
def test_the_truncated_amounts_now_arrive_whole(amount: float) -> None:
    """Each of these left the executor one unit short: int(8.12 * 10**6) == 8_119_999."""
    exact = round(amount * 10**6)
    assert int(amount * 10**6) == exact - 1  # the bug, pinned
    assert to_raw(amount, 6, Rounding.DOWN) == exact
    assert to_raw(amount, 6, Rounding.UP) == exact


def test_eighteen_decimals_are_exact() -> None:
    assert int(2.3 * 10**18) == 2_299_999_999_999_999_744  # 256 wei short
    assert to_raw(2.3, 18, Rounding.DOWN) == 2_300_000_000_000_000_000


def test_the_direction_decides_below_one_unit() -> None:
    assert to_raw(1.0000001, 6, Rounding.DOWN) == 1_000_000
    assert to_raw(1.0000001, 6, Rounding.UP) == 1_000_001


def test_float_noise_never_costs_a_unit() -> None:
    """An UP rounding must not charge a whole unit for representation noise."""
    assert 0.1 + 0.2 == 0.30000000000000004
    assert to_raw(0.1 + 0.2, 6, Rounding.UP) == 300_000
    assert to_raw(49_999.999999 + 1e-11, 6, Rounding.UP) == 49_999_999_999


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("5.000001", 5_000_001), (Decimal("5.000001"), 5_000_001), (5, 5_000_000)],
)
def test_exact_inputs_are_taken_exactly(amount: str | Decimal | int, expected: int) -> None:
    assert to_raw(amount, 6, Rounding.DOWN) == expected


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), "-0.5", Decimal("NaN")])
def test_impossible_amounts_are_refused(bad: float | str | Decimal) -> None:
    with pytest.raises(ValueError, match="montant"):
        to_raw(bad, 6, Rounding.DOWN)


def test_negative_decimals_are_refused() -> None:
    with pytest.raises(ValueError, match="décimales"):
        to_raw(1.0, -1, Rounding.DOWN)


def test_what_spends_rounds_down_and_what_authorizes_rounds_up() -> None:
    """One table, one direction per operation — README §9.2."""
    assert _ROUNDING == {
        "aave_approve": Rounding.UP,
        "aave_supply": Rounding.DOWN,
        "aave_borrow": Rounding.DOWN,
        "aave_repay": Rounding.UP,
        "aave_withdraw": Rounding.DOWN,
    }


def test_no_amount_is_converted_by_float_multiplication_anymore() -> None:
    """`units.py` itself is skipped: its docstring quotes the pattern it replaces."""
    pattern = re.compile(r"int\([^)\n]*\*\s*\(?\s*10\s*\*\*")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "units.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, f"conversion par int(montant * 10**n) : {offenders} — utiliser to_raw"
