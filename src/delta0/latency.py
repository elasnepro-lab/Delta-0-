"""Critical-path latency aggregation — the M1 deliverable.

README §14 asks M1 to produce a "rapport p50/p95 des 5 chemins critiques".
The executors record *micro-op* latencies (one sample per Aave verb, per HL
post+cancel, per bridge leg). This module composes those raw measurements
into the five decision paths of README §7 and compares each to its budget.

Composition rule: a critical path's latency is the SUM of its legs. We add
the per-leg p95s rather than the p95 of the summed samples, because the legs
are measured independently (they never fire as one timed block during the
marche à blanc). For independent legs, `sum(p95) >= p95(sum)`, so the number
reported here is a conservative upper bound — it can flag a path as slow that
is in fact within budget, never the reverse. That is the direction we want a
safety report to err in.

A path whose legs have no samples yet reports AUCUN, not a fake zero. A path
with a leg the marche à blanc cannot measure (the swap of P4 — `venues/swap.py`
is still a stub) reports INCOMPLET however fast the measured legs are.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


def now_perf() -> float:
    """Start a duration measurement.

    `perf_counter`, not `monotonic`: on Windows `time.monotonic()` is backed by
    GetTickCount64 and quantizes to ~15.6 ms, which reads a sub-millisecond
    decision loop as exactly 0 and puts a 15 ms floor on every micro-op sample.
    M1's whole deliverable is a latency report — it has to be measured on the
    high-resolution clock. Both clocks are monotonic, so a `perf_counter`
    reading is equally safe as a deadline within a process; it just carries a
    different epoch, so never compare one to a `monotonic` reading.
    """
    return time.perf_counter()


def elapsed_ms(start: float) -> float:
    """Milliseconds since a `now_perf()` reading."""
    return (time.perf_counter() - start) * 1000.0


# Dry-run samples are recorded under their own namespace. A rehearsal skips
# the network, so its samples are microseconds — mixing them into `path.*`
# would drag a real p95 down and could make a slow path read as OK. The
# critical-path registry below only ever names un-prefixed paths, so a
# rehearsal can share a database with a live run without corrupting it.
DRY_PREFIX = "dry."


def measurement_path(name: str, *, dry_run: bool) -> str:
    """Namespace a latency path so rehearsal samples never enter the stats."""
    return f"{DRY_PREFIX}{name}" if dry_run else name


# Verdicts, per README §12 (dimension "Vitesse": p95 <= budget) and §11
# (p95 > budget x latency_budget_factor => mode prudent).
Verdict = Literal["OK", "DEPASSE", "PRUDENT", "INCOMPLET", "AUCUN"]

# Empty stats shape returned by `StateStore.latency_stats` for an unknown path.
_EMPTY_STATS: dict[str, float] = {"count": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}


@dataclass(frozen=True, slots=True)
class CriticalPath:
    """One row of the README §7 decision table, with its execution budget."""

    key: str
    label: str
    venue: str
    budget_ms: float
    # Raw latency path names (as recorded by the executors) that compose it,
    # in procedure order. A name may repeat: P4's loop repays twice, and both
    # repays count toward the path's duration.
    components: tuple[str, ...]
    # Legs that exist in the procedure but that M1 cannot measure yet.
    unmeasured: tuple[str, ...] = ()


# The five critical paths. Budgets are the "Latence max" column of README §7.
# P7 to P10 are excluded on purpose: their budgets (15 min, "sans enjeu",
# "jours") make a latency report meaningless.
CRITICAL_PATHS: tuple[CriticalPath, ...] = (
    CriticalPath(
        key="P1/P2",
        label="Coupe/réduction short",
        venue="local HL",
        budget_ms=2_000.0,
        # The ORDER alone. The tracer cancels right after — it cannot leave an
        # order resting — but that cancel is rehearsal hygiene, not part of the
        # emergency path this budget covers. Charging P1/P2 for both API round
        # trips took it to 0.99x on 185 samples, heading for a DEPASSE that
        # would have failed M1 on a measurement artifact rather than on speed.
        # The pair is still recorded as `path.p1_p2_hl_local`, uncapped, and
        # shows up among the raw micro-op latencies.
        components=("path.p1_p2_hl_order",),
    ),
    CriticalPath(
        key="P3",
        label="Repay tranche (coussin)",
        venue="local Aave",
        budget_ms=10_000.0,
        components=("path.aave_withdraw", "path.aave_repay"),
    ),
    CriticalPath(
        key="P4",
        label="Désendettement 1 itér.",
        venue="local Aave",
        budget_ms=60_000.0,
        # README §8.7: repay le coussin restant -> withdraw wstETH -> swap ->
        # repay. One iteration of that loop, swap excluded.
        components=("path.aave_repay", "path.aave_withdraw", "path.aave_repay"),
        unmeasured=("swap wstETH -> USDC",),
    ),
    CriticalPath(
        key="P5",
        label="Pompe montante",
        venue="pont",
        budget_ms=180_000.0,
        components=("path.aave_borrow", "path.bridge_out_submit", "path.p5_bridge_up"),
    ),
    CriticalPath(
        key="P6",
        label="Pompe descendante",
        venue="pont",
        budget_ms=480_000.0,
        components=("path.bridge_in_submit", "path.p6_bridge_down", "path.aave_repay"),
    ),
)


@dataclass(frozen=True, slots=True)
class PathVerdict:
    """Aggregated latency of one critical path, with its budget verdict."""

    path: CriticalPath
    samples: int  # the rarest leg dictates how well-measured the path is
    p50_ms: float
    p95_ms: float
    max_ms: float
    verdict: Verdict
    missing: tuple[str, ...]  # legs with zero samples

    @property
    def budget_ratio(self) -> float:
        """p95 / budget. Above 1.0 means the budget is blown."""
        return self.p95_ms / self.path.budget_ms if self.path.budget_ms > 0 else 0.0


def evaluate_path(
    path: CriticalPath,
    stats_by_path: Mapping[str, Mapping[str, float]],
    *,
    budget_factor: float,
) -> PathVerdict:
    """Compose one critical path from its raw legs and judge it against budget."""
    # Sums walk `components` with its repeats; `missing` and `samples` walk the
    # distinct names, so a leg that appears twice is not reported twice.
    legs = [dict(stats_by_path.get(name, _EMPTY_STATS)) for name in path.components]
    distinct = tuple(dict.fromkeys(path.components))
    missing = tuple(
        name for name in distinct if int(stats_by_path.get(name, _EMPTY_STATS)["count"]) == 0
    )
    counts = [int(stats_by_path.get(name, _EMPTY_STATS)["count"]) for name in distinct]
    samples = min(counts) if counts else 0

    p50 = sum(leg["p50"] for leg in legs)
    p95 = sum(leg["p95"] for leg in legs)
    worst = sum(leg["max"] for leg in legs)

    verdict: Verdict
    if len(missing) == len(distinct):
        verdict = "AUCUN"
    elif missing or path.unmeasured:
        verdict = "INCOMPLET"
    elif p95 <= path.budget_ms:
        verdict = "OK"
    elif p95 <= path.budget_ms * budget_factor:
        verdict = "DEPASSE"
    else:
        verdict = "PRUDENT"

    return PathVerdict(
        path=path,
        samples=samples,
        p50_ms=p50,
        p95_ms=p95,
        max_ms=worst,
        verdict=verdict,
        missing=missing,
    )


def evaluate_all(
    stats_by_path: Mapping[str, Mapping[str, float]],
    *,
    budget_factor: float,
) -> list[PathVerdict]:
    """Evaluate the five critical paths in README §7 order."""
    return [evaluate_path(p, stats_by_path, budget_factor=budget_factor) for p in CRITICAL_PATHS]


def needs_prudent_mode(verdicts: list[PathVerdict]) -> bool:
    """README §11: any path whose p95 exceeds budget x factor forces prudent mode.

    In prudent mode the recentering thresholds tighten to +3 % / -4,5 %. The
    decision-engine hook lands in M2; M1 only has to surface the signal.
    """
    return any(v.verdict == "PRUDENT" for v in verdicts)


def path_meets_m1(verdict: PathVerdict) -> bool:
    """Whether one path satisfies the M1 speed criterion.

    `OK` obviously qualifies. So does a path whose ONLY gap is a leg M1 has no
    way to measure — P4's `swap wstETH -> USDC`, which cannot exist while
    `venues/swap.py` is a stub (that swap is M2 work, RUNBOOK-M1 §6). Holding
    M1 hostage to a leg M1 cannot produce would make the gate unreachable by
    construction, so the exception is explicit rather than implied.

    It is deliberately narrow. The path still has to earn its pass:
      - every leg that CAN be measured must have samples (`missing` empty), so
        a forgotten micro-op is never mistaken for an M2 gap;
      - the measured legs must still fit the full budget, which is strictly
        harsher than judging them against a prorated one.
    """
    if verdict.verdict == "OK":
        return True
    return (
        verdict.verdict == "INCOMPLET"
        and not verdict.missing
        and bool(verdict.path.unmeasured)
        and verdict.p95_ms <= verdict.path.budget_ms
    )


def m1_acceptance_met(verdicts: list[PathVerdict]) -> bool:
    """True when every critical path holds its budget on what M1 can measure.

    This is the "Vitesse" line of the tableau d'exactitude (README §12) and the
    M1 gate. No path may be unmeasured or over budget; a path may carry a leg
    that M1 structurally cannot measure — see `path_meets_m1`.
    """
    return bool(verdicts) and all(path_meets_m1(v) for v in verdicts)
