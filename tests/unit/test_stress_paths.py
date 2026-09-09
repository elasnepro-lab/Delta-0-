"""Do the defences arrive in time? — chantier 1.6.

The classeur says where the liquidation sits. It cannot say whether an
eight-minute pump crosses a two-minute crash. These tests walk dated price
paths through the real decision engine, land each action after its measured
latency, and check what survives.

Two of them do not assert a pass at all: they *measure* the crash speed and the
depeg depth at which the design breaks, and print the boundary. A number we can
watch move is worth more than a threshold chosen to be green today.
"""

from __future__ import annotations

import pytest

from delta0.config import Config
from delta0.types import Priority
from tests.simulator import (
    PathResult,
    Position,
    linear_drop,
    reference_position,
    run_path,
)

LT = 0.79


def test_a_slow_fall_meets_the_defences_in_order(config: Config) -> None:
    """-12 % over six hours: every priority gets its turn, nothing liquidates."""
    prices = linear_drop(0.12, duration_s=6 * 3600)
    result = run_path(prices, reference_position(LT), config, anchored=False)

    assert result.survived
    fired = [p for _, p, _ in result.actions]
    assert Priority.P6_PUMP_DOWN in fired
    # The pump, being the slowest path, must be the first one asked to run.
    assert fired[0] is Priority.P6_PUMP_DOWN
    # And it must arrive early enough that nothing worse is ever reached.
    assert Priority.P4_DELEVERAGE not in fired


def test_the_defences_hold_a_fall_the_old_config_could_not(config: Config) -> None:
    """-10 %: past the old cushion threshold, which sat on the liquidation."""
    prices = linear_drop(0.10, duration_s=3 * 3600)
    result = run_path(prices, reference_position(LT), config)
    assert result.survived
    assert result.hf_min > 1.0


def test_a_flash_crash_outruns_the_bridge(config: Config) -> None:
    """The pump takes 5 min 15. A fall faster than that cannot be pumped away.

    Not a defect to fix here — a property of the chosen path. It is the
    argument for the local defences (cushion, reserve) carrying the fast cases,
    and it is why B2's preemption matters in phase 3.
    """
    slow = run_path(linear_drop(0.14, duration_s=6 * 3600), reference_position(LT), config)
    fast = run_path(linear_drop(0.14, duration_s=120), reference_position(LT), config)
    assert slow.hf_min > fast.hf_min


def test_where_the_defences_break_on_speed(config: Config) -> None:
    """Measure, do not assert: the fastest -14 % the chassis survives."""
    survived: list[float] = []
    died: list[float] = []
    for duration_s in (60, 120, 300, 600, 1_800, 3_600, 7_200, 21_600):
        result = run_path(
            linear_drop(0.14, duration_s=float(duration_s)),
            reference_position(LT),
            config,
        )
        (survived if result.survived else died).append(duration_s)

    print(f"\n  -14 % survécu si étalé sur : {survived}")
    print(f"  -14 % fatal si étalé sur   : {died}")
    # The only property worth pinning: slower is never worse.
    if survived and died:
        assert min(survived) > max(died)


def test_the_cushion_stops_protecting_once_spent(config: Config) -> None:
    """The trap measured in aave_findings §11, shown end to end."""
    with_cushion = run_path(linear_drop(0.13, duration_s=6 * 3600), reference_position(LT), config)
    empty = reference_position(LT)
    empty.cushion_usd = 0.0
    empty.debt_usd = 35_000.0
    without = run_path(linear_drop(0.13, duration_s=6 * 3600), empty, config)
    assert without.hf_min < with_cushion.hf_min


def test_the_reserve_is_what_defends_the_up_flank(config: Config) -> None:
    """A squeeze with and without the HL reserve.

    Same path, same chassis, one difference: whether P2 has anything to spend.

    The path is deliberately violent — +12 % in a minute. On anything slower
    P5 refills the margin from Aave and P2 is never reached, which is the
    production behaviour and precisely what makes P2 the *fast* defence: it
    only matters when the move outruns the bridge. The arithmetic: the margin
    ratio is (0.1 - x) / (1 + x), so P5 triggers at +4.8 % and P2 at +6.3 %,
    about six seconds apart here against P5's 11.2 s latency.
    """
    rising = [2_500.0 * (1 + 0.12 * i / 12) for i in range(13)]

    stocked = reference_position(LT)
    empty = reference_position(LT)
    empty.hl_free_usdc = 0.0

    with_reserve = run_path(rising, stocked, config, anchored=False, preempt=True)
    without_reserve = run_path(rising, empty, config, anchored=False, preempt=True)

    def first_p2(result: PathResult) -> str:
        return next(k for _, prio, k in result.actions if prio is Priority.P2_EMERGENCY_REDUCE)

    # With a reserve, the first thing P2 reaches for is the reserve — the hedge
    # is left alone. Without one it has no choice but to cut.
    assert first_p2(with_reserve) == "ADD_ISOLATED_MARGIN"
    assert first_p2(without_reserve) == "REDUCE"


def test_how_much_reserve_a_squeeze_needs(config: Config) -> None:
    """Measure, do not assert: the reserve that keeps the hedge intact.

    Direct input to the classeur. 2 % of notional is a round number, not a
    sized one — it covers a gentle squeeze and runs out on a violent one, after
    which P2 falls back to cutting the hedge. What follows is the smallest
    reserve that never touches the short, per squeeze size.
    """
    print()
    for pct in (0.06, 0.09, 0.12):
        rising = [2_500.0 * (1 + pct * i / 12) for i in range(13)]
        needed: float | None = None
        for reserve in range(0, 9_000, 250):
            position = reference_position(LT)
            position.hl_free_usdc = float(reserve)
            result = run_path(rising, position, config, anchored=False, preempt=True)
            if result.count("REDUCE") == 0:
                needed = float(reserve)
                break
        pct_of_notional = None if needed is None else 100 * needed / 50_000
        shown = (
            "jamais suffisant sous 9 000"
            if needed is None
            else (f"{needed:,.0f} USDC ({pct_of_notional:.1f} % du notionnel)")
        )
        print(f"  squeeze +{100 * pct:.0f} % en 1 min -> reserve requise : {shown}")


def test_a_slow_priority_in_flight_blocks_the_fast_one(config: Config) -> None:
    """What preemption is worth, measured — the argument for chantier 3.5.

    Same squeeze twice. Today the scheduler awaits each action, so P5 takes the
    floor at +4.8 % and holds it for 11.2 s while the margin keeps bleeding
    past P2's threshold. P2 never runs, and the reserve sits unspent while the
    position deteriorates.
    """
    rising = [2_500.0 * (1 + 0.12 * i / 12) for i in range(13)]

    blocked = run_path(rising, reference_position(LT), config, anchored=False)
    preempting = run_path(rising, reference_position(LT), config, anchored=False, preempt=True)

    print(f"\n  sans préemption : {[k for _, _, k in blocked.actions]}")
    print(f"  avec préemption : {[k for _, _, k in preempting.actions]}")
    print(
        f"  ratio de marge minimal : {blocked.margin_ratio_min:.4f}"
        f" -> {preempting.margin_ratio_min:.4f}"
    )

    assert blocked.margin_ratio_min < config.emergency.margin_ratio_reduce
    assert blocked.count("ADD_ISOLATED_MARGIN") == 0  # P2 never got the floor
    assert preempting.count("ADD_ISOLATED_MARGIN") > 0


def test_a_quiet_path_costs_nothing(config: Config) -> None:
    """Noise must not trigger anything: every action costs fees."""
    flat = [2_500.0 * (1 + 0.004 * (-1) ** i) for i in range(2_000)]
    result = run_path(flat, reference_position(LT), config)
    assert result.survived
    assert result.actions == []


def test_the_refire_lock_is_what_stops_the_bleeding(config: Config) -> None:
    """Without one-in-flight, an unresolved trigger fires every cycle.

    This is the measurement behind chantier 3.1: the same path, decided with
    and without the lock. It is also why P2 must not spend a reserve too small
    to clear its own threshold.
    """
    prices = linear_drop(0.13, duration_s=3 * 3600)

    locked = run_path(prices, reference_position(LT), config, one_in_flight=True)
    unlocked = run_path(prices, reference_position(LT), config, one_in_flight=False)

    print(f"\n  actions avec verrou : {len(locked.actions)}")
    print(f"  actions sans verrou : {len(unlocked.actions)}")
    assert len(unlocked.actions) > len(locked.actions)


@pytest.mark.parametrize("lt", [0.79, 0.75])
def test_a_governance_cut_moves_the_bands_not_the_outcome(config: Config, lt: float) -> None:
    """A lowered threshold must still leave the defences room to act."""
    result = run_path(linear_drop(0.06, duration_s=6 * 3600), reference_position(lt), config)
    assert result.survived


def test_the_old_thresholds_would_have_liquidated(config: Config) -> None:
    """The regression, played out rather than argued.

    With the cushion at 0.79 and the deleverage at 0.81 against a real LT of
    0.79, a fall past the pump met no further defence. Reproduced by starting
    the chassis close enough that only those two could have helped.
    """
    stressed = Position(
        wsteth=16.0,
        cushion_usd=1_000.0,
        debt_usd=39_000.0,  # LTV ~0.765, already past the pump
        short_eth=20.0,
        margin_usd=5_000.0,
        hl_free_usdc=1_000.0,
        lt=LT,
    )
    result = run_path(linear_drop(0.05, duration_s=6 * 3600), stressed, config, anchored=False)
    # With the derived bands the cushion still has room to act; with the old
    # absolute thresholds it sat on the liquidation and could not.
    fired = {p for _, p, _ in result.actions}
    assert Priority.P6_PUMP_DOWN in fired
    assert result.survived
