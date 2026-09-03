"""Critical-path latency aggregation — composition, verdicts, M1 gate."""

from __future__ import annotations

import time

import pytest

from delta0.latency import (
    CRITICAL_PATHS,
    CriticalPath,
    elapsed_ms,
    evaluate_all,
    evaluate_path,
    m1_acceptance_met,
    needs_prudent_mode,
    now_perf,
    path_meets_m1,
)

_FACTOR = 1.5


def _stats(count: int, p50: float, p95: float, worst: float | None = None) -> dict[str, float]:
    return {
        "count": float(count),
        "p50": p50,
        "p95": p95,
        "max": worst if worst is not None else p95,
    }


def _path_by_key(key: str) -> CriticalPath:
    return next(p for p in CRITICAL_PATHS if p.key == key)


def test_registry_covers_the_five_critical_paths() -> None:
    assert [p.key for p in CRITICAL_PATHS] == ["P1/P2", "P3", "P4", "P5", "P6"]


def test_budgets_match_readme_section_7() -> None:
    budgets = {p.key: p.budget_ms for p in CRITICAL_PATHS}
    assert budgets["P1/P2"] == 2_000
    assert budgets["P3"] == 10_000
    assert budgets["P4"] == 60_000
    assert budgets["P5"] == 180_000
    assert budgets["P6"] == 480_000


def test_no_samples_reports_aucun_not_a_fake_zero() -> None:
    v = evaluate_path(_path_by_key("P1/P2"), {}, budget_factor=_FACTOR)
    assert v.verdict == "AUCUN"
    assert v.samples == 0
    assert v.missing == ("path.p1_p2_hl_local",)


def test_within_budget_is_ok() -> None:
    stats = {"path.p1_p2_hl_local": _stats(120, 300.0, 480.0)}
    v = evaluate_path(_path_by_key("P1/P2"), stats, budget_factor=_FACTOR)
    assert v.verdict == "OK"
    assert v.samples == 120
    assert v.p95_ms == 480.0
    assert v.budget_ratio == pytest.approx(0.24)


def test_over_budget_but_under_factor_is_depasse() -> None:
    # 2 500 ms: above the 2 s budget, below 2 s x 1.5.
    stats = {"path.p1_p2_hl_local": _stats(50, 1_800.0, 2_500.0)}
    v = evaluate_path(_path_by_key("P1/P2"), stats, budget_factor=_FACTOR)
    assert v.verdict == "DEPASSE"


def test_over_budget_times_factor_is_prudent() -> None:
    stats = {"path.p1_p2_hl_local": _stats(50, 2_900.0, 3_100.0)}
    v = evaluate_path(_path_by_key("P1/P2"), stats, budget_factor=_FACTOR)
    assert v.verdict == "PRUDENT"


def test_exactly_at_budget_is_ok_not_depasse() -> None:
    stats = {"path.p1_p2_hl_local": _stats(10, 1_000.0, 2_000.0)}
    assert evaluate_path(_path_by_key("P1/P2"), stats, budget_factor=_FACTOR).verdict == "OK"


def test_exactly_at_budget_times_factor_is_depasse_not_prudent() -> None:
    stats = {"path.p1_p2_hl_local": _stats(10, 1_000.0, 3_000.0)}
    assert evaluate_path(_path_by_key("P1/P2"), stats, budget_factor=_FACTOR).verdict == "DEPASSE"


def test_legs_are_summed() -> None:
    stats = {
        "path.aave_withdraw": _stats(40, 1_000.0, 1_500.0, 2_000.0),
        "path.aave_repay": _stats(40, 900.0, 1_200.0, 1_800.0),
    }
    v = evaluate_path(_path_by_key("P3"), stats, budget_factor=_FACTOR)
    assert v.p50_ms == 1_900.0
    assert v.p95_ms == 2_700.0
    assert v.max_ms == 3_800.0
    assert v.verdict == "OK"


def test_rarest_leg_dictates_the_sample_count() -> None:
    stats = {
        "path.aave_withdraw": _stats(40, 1_000.0, 1_500.0),
        "path.aave_repay": _stats(7, 900.0, 1_200.0),
    }
    assert evaluate_path(_path_by_key("P3"), stats, budget_factor=_FACTOR).samples == 7


def test_partially_measured_path_is_incomplet() -> None:
    stats = {"path.aave_withdraw": _stats(40, 1_000.0, 1_500.0)}
    v = evaluate_path(_path_by_key("P3"), stats, budget_factor=_FACTOR)
    assert v.verdict == "INCOMPLET"
    assert v.missing == ("path.aave_repay",)


def test_p4_stays_incomplet_even_when_measured_legs_are_fast() -> None:
    # The swap leg has no measurement in M1: a fast repay+withdraw must not
    # be allowed to look like a validated P4.
    stats = {
        "path.aave_repay": _stats(40, 500.0, 800.0),
        "path.aave_withdraw": _stats(40, 500.0, 800.0),
    }
    v = evaluate_path(_path_by_key("P4"), stats, budget_factor=_FACTOR)
    assert v.verdict == "INCOMPLET"
    assert v.missing == ()
    assert v.path.unmeasured


def test_bridge_paths_compose_submit_and_credit_wait() -> None:
    stats = {
        "path.aave_borrow": _stats(20, 1_000.0, 2_000.0),
        "path.bridge_out_submit": _stats(14, 1_500.0, 3_000.0),
        "path.p5_bridge_up": _stats(14, 100_000.0, 140_000.0),
    }
    v = evaluate_path(_path_by_key("P5"), stats, budget_factor=_FACTOR)
    assert v.p95_ms == 145_000.0
    assert v.samples == 14
    assert v.verdict == "OK"


def test_evaluate_all_returns_one_verdict_per_path_in_order() -> None:
    verdicts = evaluate_all({}, budget_factor=_FACTOR)
    assert [v.path.key for v in verdicts] == [p.key for p in CRITICAL_PATHS]


def test_needs_prudent_mode_only_on_prudent() -> None:
    slow = {"path.p1_p2_hl_local": _stats(10, 4_000.0, 5_000.0)}
    assert needs_prudent_mode(evaluate_all(slow, budget_factor=_FACTOR)) is True
    assert needs_prudent_mode(evaluate_all({}, budget_factor=_FACTOR)) is False


def test_m1_gate_requires_every_path_ok() -> None:
    assert m1_acceptance_met(evaluate_all({}, budget_factor=_FACTOR)) is False
    assert m1_acceptance_met([]) is False


def test_m1_gate_passes_when_all_measured_paths_hold_budget() -> None:
    # P4 carries an unmeasured leg, so the gate can only pass on a registry
    # where every path is fully measurable. Verify the rule itself.
    fast = _stats(100, 10.0, 20.0)
    verdicts = [
        evaluate_path(p, dict.fromkeys(p.components, fast), budget_factor=_FACTOR)
        for p in CRITICAL_PATHS
        if not p.unmeasured
    ]
    assert m1_acceptance_met(verdicts) is True


def test_budget_ratio_is_zero_for_a_zero_budget_path() -> None:
    path = CriticalPath(
        key="X",
        label="test",
        venue="test",
        budget_ms=0.0,
        components=("path.x",),
    )
    v = evaluate_path(path, {"path.x": _stats(1, 5.0, 5.0)}, budget_factor=_FACTOR)
    assert v.budget_ratio == 0.0


def test_clock_resolves_below_the_windows_monotonic_quantum() -> None:
    """The measurement clock must be finer than `time.monotonic` on Windows.

    `time.monotonic()` quantizes to ~15.6 ms there. A 3 ms sleep measured on
    it reads as 0 or 15.6, which would make the whole M1 latency report
    meaningless. This test fails if someone swaps the clock back.
    """
    start = now_perf()
    time.sleep(0.003)
    measured = elapsed_ms(start)
    assert 1.0 < measured < 100.0


def test_elapsed_ms_is_monotonically_non_decreasing() -> None:
    start = now_perf()
    first = elapsed_ms(start)
    second = elapsed_ms(start)
    assert second >= first >= 0.0


def test_a_repeated_leg_counts_twice_in_the_sum_once_in_the_report() -> None:
    # P4's loop repays twice; both repays cost time, but the operator should
    # not be told "aucune mesure pour aave_repay, aave_repay".
    p4 = _path_by_key("P4")
    assert p4.components.count("path.aave_repay") == 2

    stats = {
        "path.aave_repay": _stats(30, 1_000.0, 1_500.0),
        "path.aave_withdraw": _stats(30, 2_000.0, 2_500.0),
    }
    v = evaluate_path(p4, stats, budget_factor=_FACTOR)
    assert v.p50_ms == 4_000.0  # 1000 + 2000 + 1000
    assert v.p95_ms == 5_500.0  # 1500 + 2500 + 1500
    assert v.samples == 30

    v_missing = evaluate_path(p4, {}, budget_factor=_FACTOR)
    assert v_missing.verdict == "AUCUN"
    assert v_missing.missing == ("path.aave_repay", "path.aave_withdraw")


# --- M1 acceptance with a structurally unmeasurable leg -----------------------
#
# P4's swap leg cannot exist while `venues/swap.py` is a stub (M2 work,
# RUNBOOK-M1 §6). Requiring it would make the M1 gate unreachable, so the
# exception is explicit — and narrow.


def _all_legs_fast() -> dict[str, dict[str, float]]:
    """Every raw leg measured and quick enough for any budget."""
    names = {name for path in CRITICAL_PATHS for name in path.components}
    return {name: _stats(50, 100.0, 200.0, 300.0) for name in names}


def test_path_with_only_an_unmeasurable_leg_still_passes() -> None:
    verdicts = evaluate_all(_all_legs_fast(), budget_factor=1.5)
    p4 = next(v for v in verdicts if v.path.key == "P4")
    assert p4.verdict == "INCOMPLET"  # the swap leg is still declared missing
    assert p4.missing == ()  # but nothing measurable was skipped
    assert path_meets_m1(p4)
    assert m1_acceptance_met(verdicts)


def test_a_forgotten_micro_op_is_not_excused() -> None:
    """The exemption covers M2 gaps, never an un-run micro-op."""
    stats = _all_legs_fast()
    del stats["path.aave_withdraw"]  # a leg M1 *can* measure, simply not run
    verdicts = evaluate_all(stats, budget_factor=1.5)
    p4 = next(v for v in verdicts if v.path.key == "P4")
    assert not path_meets_m1(p4)
    assert not m1_acceptance_met(verdicts)


def test_an_unmeasurable_leg_does_not_excuse_being_over_budget() -> None:
    """Exempting the swap leg must not exempt the measured ones from budget."""
    slow = {
        name: _stats(50, 40_000.0, 40_000.0, 40_000.0)
        for path in CRITICAL_PATHS
        for name in path.components
    }
    verdicts = evaluate_all(slow, budget_factor=1.5)
    p4 = next(v for v in verdicts if v.path.key == "P4")
    assert p4.p95_ms > p4.path.budget_ms
    assert not path_meets_m1(p4)
    assert not m1_acceptance_met(verdicts)
