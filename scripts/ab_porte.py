"""A/B de la porte de régime (chantier 7.4) : calibrer `safety_margin_bps` et `spread_full_bps`.

Usage :
    python scripts/ab_porte.py                  # grille complète, segment FIDÈLE
    python scripts/ab_porte.py --quick          # une campagne OFF et une ON, pour mesurer le temps
    python scripts/ab_porte.py --workers 4

Chaque campagne part d'un bilan posé EXACTEMENT sur la cible du solveur à la
première minute, avec le vrai `decide()`, la préemption active (sans elle le
montage meurt, README §6 et backtest du 2026-09-17), et la porte de régime telle
que le README §8.9 la décrit depuis la seconde revue finance : funding comparé au
seuil de rentabilité f* = target_ltv x emprunt_30j - staking_30j.

Ce que le script rend, par campagne : survie et lieu de la mort, rendement par
année civile ET sur les douze derniers mois (jamais une moyenne seule), coûts,
funding encaissé, intérêts payés, nombre de changements de régime et de
tranches, et la part du temps passée à chaque exposition. Les résultats vont
dans `data/backtest/ab_porte-<date>.json`, hors du dépôt.

Réserve qui vaut pour tout chiffre sorti d'ici : neuf paramètres de coût sur dix
sont supposés (`backtest.costs.DEFAULT.unverified()`), et le mark Hyperliquid
est remplacé par le mark Binance. Les écarts ENTRE campagnes valent mieux que
leurs niveaux.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from backtest import ledger  # noqa: E402
from backtest.cache import Month  # noqa: E402
from backtest.costs import DEFAULT  # noqa: E402
from backtest.engine import Engine  # noqa: E402
from backtest.ledger import Book, Moment  # noqa: E402
from backtest.timeline import Minute, Timeline  # noqa: E402
from delta0.config import Config, load_config  # noqa: E402
from delta0.decision import target_state  # noqa: E402

DATA = REPO / "data" / "backtest"
CAPITAL = 20_000.0
START: Month = (2023, 7)  # premier mois entier du segment FIDÈLE
END: Month = (2026, 8)  # dernier mois en cache
DAY_MS = 86_400_000
DAYS_PER_YEAR = 365

SAFETY_GRID = (0, 100, 200, 300, 400)
FULL_GRID = (300, 500, 700, 900)
TARGETS = (0.675, 0.625)


@dataclass(frozen=True, slots=True)
class Campaign:
    name: str
    target_ltv: float
    regime: bool
    safety_margin_bps: int = 0
    spread_full_bps: int = 500


def config_for(base: Config, run: Campaign) -> Config:
    """La config de l'exemple, cible et bandes changées, revalidée en entier.

    `model_copy` ne relancerait pas les validateurs : une exposition qui ne
    correspond plus à sa cible passerait. On repasse par `model_validate`.
    """
    raw: dict[str, Any] = base.model_dump(mode="json")
    full = 1.0 / (1.0 - run.target_ltv + 1.0 / base.short_leverage)
    raw["target_ltv"] = run.target_ltv
    raw["exposure_mult"] = full
    raw["exposure_mult_half"] = full * base.exposure_mult_half / base.exposure_mult
    raw["regime"]["safety_margin_bps"] = run.safety_margin_bps
    raw["regime"]["spread_full_bps"] = run.spread_full_bps
    return Config.model_validate(raw)


def book_at_target(config: Config, first: Minute) -> Book:
    """Le bilan du solveur à la première minute, rien de plus, rien de moins."""
    cushion = CAPITAL * config.cushion_pct
    cible = target_state(CAPITAL, config, cushion_usd=cushion)
    eth, mark = ledger.prices(first, Moment.CLOSE)
    oracle = ledger.oracle_price(eth, first.ratio)
    wsteth = cible.spot_target_usd / oracle
    placed = (
        cible.spot_target_usd
        + cushion
        + cible.margin_target_usd
        + cible.reserve_target_usd
        - cible.debt_target_usd
    )
    return Book(
        wsteth=wsteth,
        cushion_usd=cushion,
        debt_usd=cible.debt_target_usd,
        short_eth=wsteth * first.ratio,
        short_entry_px=mark,
        margin_usd=cible.margin_target_usd,
        hl_free_usdc=cible.reserve_target_usd,
        wallet_usdc=CAPITAL - placed,
        gas_eth=0.05,
        lt=ledger.LT_TODAY,
    )


@dataclass(slots=True)
class Recorder:
    """Relève l'équité une fois par jour, sur le bilan tel que le moteur l'a laissé."""

    book: Book
    config: Config
    daily: list[tuple[int, float]] = field(default_factory=list)
    last: Minute | None = None

    def equity(self, minute: Minute) -> float:
        snap = ledger.snapshot(
            self.book,
            minute,
            Moment.CLOSE,
            funding_last_hour=0.0,
            funding_30d_annualized=0.0,
            maintenance_margin=self.config.maintenance_margin,
            ltv_max=ledger.LTV_MAX_TODAY,
        )
        return snap.equity

    def walk(self, minutes: Iterator[Minute]) -> Iterator[Minute]:
        for minute in minutes:
            # Au retour du générateur, le moteur a fini la minute précédente :
            # c'est elle qu'on relève quand un jour se ferme.
            if self.last is not None and minute.ts_ms // DAY_MS != self.last.ts_ms // DAY_MS:
                self.daily.append((self.last.ts_ms, self.equity(self.last)))
            self.last = minute
            yield minute


@dataclass(slots=True)
class WrappedTimeline:
    timeline: Timeline
    recorder: Recorder

    def walk(self, start: Month, end: Month) -> Iterator[Minute]:
        return self.recorder.walk(self.timeline.walk(start, end))


def yearly_returns(daily: list[tuple[int, float]]) -> dict[str, float]:
    """Rendement par année civile, rapporté à l'équité du début de l'année."""
    by_year: dict[int, list[float]] = {}
    for ts_ms, equity in daily:
        by_year.setdefault(datetime.fromtimestamp(ts_ms / 1000, UTC).year, []).append(equity)
    out: dict[str, float] = {}
    for year, values in sorted(by_year.items()):
        days = len(values)
        raw = values[-1] / values[0] - 1.0
        out[str(year)] = raw * DAYS_PER_YEAR / days if days < DAYS_PER_YEAR else raw
    return out


def last_twelve_months(daily: list[tuple[int, float]]) -> float | None:
    if len(daily) <= DAYS_PER_YEAR:
        return None
    return daily[-1][1] / daily[-(DAYS_PER_YEAR + 1)][1] - 1.0


def run(campaign: Campaign) -> dict[str, Any]:
    started = time.monotonic()
    config = config_for(load_config(REPO / "config.yaml.example"), campaign)
    first = next(Timeline(root=DATA).walk(START, START))
    book = book_at_target(config, first)
    recorder = Recorder(book=book, config=config)
    engine = Engine(config=config, costs=DEFAULT, preempt=True, regime=campaign.regime)
    journal = engine.run(WrappedTimeline(Timeline(root=DATA), recorder), book, START, END)  # type: ignore[arg-type]
    assert recorder.last is not None
    final_equity = recorder.equity(recorder.last)
    years = (recorder.last.ts_ms - first.ts_ms) / (DAY_MS * 365)

    # Part du temps à chaque exposition voulue, d'après les bascules de la porte.
    exposure_time: dict[str, float] = {}
    if campaign.regime:
        changes = [(first.ts_ms, config.exposure_mult), *journal.regime_changes]
        bounds = [*(ts for ts, _ in changes[1:]), recorder.last.ts_ms]
        for (ts, target), until in zip(changes, bounds, strict=True):
            key = f"{target:.3f}"
            exposure_time[key] = exposure_time.get(key, 0.0) + (until - ts)
        span = recorder.last.ts_ms - first.ts_ms
        exposure_time = {k: v / span for k, v in exposure_time.items()}

    liquidation = journal.liquidation
    charge = journal.charged()
    return {
        "campaign": campaign.name,
        "target_ltv": campaign.target_ltv,
        "regime": campaign.regime,
        "safety_margin_bps": campaign.safety_margin_bps,
        "spread_full_bps": campaign.spread_full_bps,
        "survived": journal.survived,
        "liquidation": None
        if liquidation is None
        else {
            "date": datetime.fromtimestamp(liquidation.ts_ms / 1000, UTC).date().isoformat(),
            "venue": str(liquidation.venue),
            "hf": liquidation.hf,
            "margin_ratio": liquidation.margin_ratio,
        },
        "years": years,
        "final_equity": final_equity,
        "annualized": (final_equity / CAPITAL) ** (1 / years) - 1.0 if final_equity > 0 else -1.0,
        "by_year": yearly_returns(recorder.daily),
        "last_12_months": last_twelve_months(recorder.daily),
        "costs_usd": charge.total_usd,
        "funding_usd": journal.funding_received_usd,
        "interest_usd": journal.interest_paid_usd,
        "regime_changes": len(journal.regime_changes),
        "regime_steps": journal.count("REGIME_STEP"),
        "regime_suppressed": journal.regime_suppressed,
        "regime_rate_limited": journal.regime_rate_limited,
        "exposure_time": exposure_time,
        "hf_min": journal.hf_min,
        "margin_ratio_min": journal.margin_ratio_min,
        "actions": {kind: journal.count(kind) for kind in sorted({d.kind for d in journal.done})},
        "seconds": time.monotonic() - started,
    }


def grid(quick: bool) -> list[Campaign]:
    if quick:
        return [
            Campaign("OFF-0.675", 0.675, regime=False),
            Campaign("ON-0.675-s0-f500", 0.675, regime=True),
        ]
    runs: list[Campaign] = []
    for target in TARGETS:
        runs.append(Campaign(f"OFF-{target}", target, regime=False))
        for safety in SAFETY_GRID:
            for full in FULL_GRID:
                if safety < full:
                    runs.append(
                        Campaign(f"ON-{target}-s{safety}-f{full}", target, True, safety, full)
                    )
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    campaigns = grid(args.quick)
    out = DATA / f"ab_porte-{date.today().isoformat()}{'-quick' if args.quick else ''}.json"
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(run, campaigns):
            results.append(result)
            status = "survit" if result["survived"] else f"MORT {result['liquidation']['date']}"
            print(
                f"{result['campaign']:<24} {status:<18} "
                f"{100 * result['annualized']:6.2f} %/an  "
                f"12 m {100 * (result['last_12_months'] or 0):6.2f} %  "
                f"coûts {result['costs_usd']:8.0f} $  ({result['seconds']:.0f} s)",
                flush=True,
            )
            out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n{len(results)} campagnes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
