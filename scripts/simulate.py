"""Five-year projection of the chassis, across capital and target LTV.

A scenario, not a forecast. Every assumption below is a named constant so it
can be argued with and re-run — that is the point. `--json` emits the raw
figures for the artifact.

    uv run python scripts/simulate.py
    uv run python scripts/simulate.py --json

Chain per year, as asked:
    rendement brut -> frais de transaction -> net -> provision liquidation
    -> net final, recomposé dans le capital de l'année suivante.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from classeur import BORROW_APR, FUNDING_APR, STAKING_APR, solve  # noqa: E402

from delta0.config import load_config  # noqa: E402

YEARS = 5
CAPITALS = (20_000.0, 25_000.0, 30_000.0, 35_000.0)
TARGETS = (0.700, 0.675, 0.650)
LT = 0.79

# --- Transaction costs, per year, scaled on the notional --------------------
# Reference point is the audit's §F4 at a 50 000 $ notional. Everything here is
# linear in notional, which understates a small chassis (the 1 $ bridge fee and
# the gas do not shrink) and is therefore mildly optimistic below 50 000 $.
REF_NOTIONAL = 50_000.0
RECENTRES_PER_YEAR = 45.0  # ETH crosses ±4.5 % / -6 % from an anchor 30-60 x/yr
COST_PER_RECENTRE = 8.0  # bridge 1 $ + gas + HL fee on the retaille + swap
TRUEUPS_PER_YEAR = 1.4  # staking drifts the ratio +2.7 %/yr against a 2 % band
COST_PER_TRUEUP = 6.0
SKIMS_PER_YEAR = 12.0
COST_PER_SKIM = 10.0  # bridge round trip + HL fees on the recomposition
GAS_PER_YEAR = 60.0  # Aave legs outside the events above

# --- Liquidation model -------------------------------------------------------
# THE most arguable number here. Annual frequency of an ETH fall exceeding `x`
# FAST ENOUGH to beat the defences — the stress harness puts that boundary at
# roughly a quarter of an hour. Calibrated so that a 10 % move happens about
# 1.2 x/yr and decaying as a power law, which is the shape fat-tailed daily
# returns actually take. Re-derive it from real history in M2b; until then it
# is an assumption on display, not a measurement.
LIQ_FREQ_AT_10PCT = 1.2
LIQ_DECAY = 3.4
# Cost of one "clean" liquidation: close factor 50 % at a 7.2 % penalty, so
# 3.6 % of the debt, per audit §A3.
LIQ_COST_PER_DEBT = 0.036


def liquidation_frequency(band: float) -> float:
    """Expected liquidations per year for a given (positive) band width."""
    if band <= 0:
        return LIQ_FREQ_AT_10PCT * 10
    return LIQ_FREQ_AT_10PCT * (band / 0.10) ** (-LIQ_DECAY)


@dataclass
class YearRow:
    year: int
    capital: float
    spot: float
    debt: float
    band_empty: float
    gross: float
    fees: float
    net: float
    liq_provision: float
    net_final: float

    @property
    def yield_pct(self) -> float:
        return 100 * self.net_final / self.capital


def run_scenario(config_path: Path, capital: float, target_ltv: float) -> list[YearRow]:
    config = load_config(config_path)
    rows: list[YearRow] = []
    current = capital

    for year in range(1, YEARS + 1):
        cfg = config.model_copy(update={"capital_usd": current})
        chassis = solve(cfg, lt=LT, target_ltv=target_ltv)

        gross = FUNDING_APR * chassis.spot + STAKING_APR * chassis.spot - BORROW_APR * chassis.debt

        scale = chassis.spot / REF_NOTIONAL
        fees = scale * (
            RECENTRES_PER_YEAR * COST_PER_RECENTRE
            + TRUEUPS_PER_YEAR * COST_PER_TRUEUP
            + SKIMS_PER_YEAR * COST_PER_SKIM
            + GAS_PER_YEAR
        )

        net = gross - fees
        band_empty = chassis.band(cushion_intact=False)
        liq_provision = liquidation_frequency(band_empty) * LIQ_COST_PER_DEBT * chassis.debt
        net_final = net - liq_provision

        rows.append(
            YearRow(
                year=year,
                capital=current,
                spot=chassis.spot,
                debt=chassis.debt,
                band_empty=band_empty,
                gross=gross,
                fees=fees,
                net=net,
                liq_provision=liq_provision,
                net_final=net_final,
            )
        )
        current += net_final  # recomposed, per the v1 skim policy

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO / "config.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results: dict[str, dict[str, list[dict[str, float]]]] = {}
    for capital in CAPITALS:
        key = f"{int(capital)}"
        results[key] = {}
        for target in TARGETS:
            rows = run_scenario(args.config, capital, target)
            results[key][f"{target:.3f}"] = [asdict(r) for r in rows]

    if args.json:
        print(json.dumps(results, indent=1))
        return

    for capital in CAPITALS:
        print(f"\n{'=' * 78}\n  Capital initial {capital:,.0f} $\n{'=' * 78}")
        for target in TARGETS:
            rows = run_scenario(args.config, capital, target)
            last = rows[-1]
            total = sum(r.net_final for r in rows)
            print(f"\n  cible {target:.3f} — bande coussin vide {-100 * rows[0].band_empty:.2f} %")
            print(
                f"  {'an':>4}{'capital':>11}{'brut':>9}{'frais':>8}"
                f"{'net':>9}{'prov. liq':>11}{'net final':>11}{'rdt':>8}"
            )
            for r in rows:
                print(
                    f"  {r.year:>4}{r.capital:>11,.0f}{r.gross:>9,.0f}{-r.fees:>8,.0f}"
                    f"{r.net:>9,.0f}{-r.liq_provision:>11,.0f}"
                    f"{r.net_final:>11,.0f}{r.yield_pct:>7.1f}%"
                )
            print(
                f"  {'cumul':>4}{'':>11}{'':>9}{'':>8}{'':>9}{'':>11}"
                f"{total:>11,.0f}   capital final {last.capital + last.net_final:,.0f} $"
            )


if __name__ == "__main__":
    main()
