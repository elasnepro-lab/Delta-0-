"""Generate the reference balance sheet from the config — the Model C workbook.

A workbook maintained beside the code is how a liquidation threshold of 0.81
survived for months while Arbitrum applied 0.79: the two never had to agree.
This derives every figure from `config.yaml` and the on-chain threshold, so a
divergence becomes impossible rather than merely unlikely.

    uv run python scripts/classeur.py                 # current config
    uv run python scripts/classeur.py --compare       # candidate target LTVs
    uv run python scripts/classeur.py --lt 0.75       # a governance cut

The down-flank bands are printed three ways on purpose. With the cushion
intact; with the cushion GONE but the debt unchanged — the worst reading, kept
because it bounds a cushion lost rather than spent; and after P3, the cushion
spent on the debt, which is what the table actually does. The middle one used
to be labelled "coussin vide" as if it were the third (revue finance m2).

The up flank is printed too (m8): the price rises at which the cruise floor,
the pump, P2 and the liquidation fire, from the target margin ratio, and the
liquidation once P2 has poured the reserve in.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from delta0.config import Config, load_config
from delta0.decision import derive_bands, target_state

REPO = Path(__file__).resolve().parents[1]

# Read on-chain 2026-09-08, block 503134105 — memory/aave_findings.md §9.
# Re-read it with scripts/read_aave_params.py before trusting any of this.
DEFAULT_LT = 0.79
# USDC as collateral on Aave Arbitrum, read 2026-10-05 (revue finance m1): the
# cushion liquidates at its own threshold, not the wstETH's.
LT_USDC = 0.78

# Carry assumptions, read on 2026-10-05 (revue finance du même jour, m9). A
# SCENARIO, not an expectation: these are 30-day readings, and the funding went
# from 21.6 %/yr in 2023 to 5.25 % in 2026 — one window says nothing of the next.
# Funding: Hyperliquid ETH, 720 hourly points paginated. Borrow: Aave v3 Arbitrum
# USDC variable APR. Staking: inferred from three readings of the oracle ratio.
FUNDING_APR = 0.1019
STAKING_APR = 0.022
BORROW_APR = 0.03944

# A provision is a rate on the base that makes it grow, plus what does not grow.
# They used to be fixed dollars, roughly right at 20 k$ and absurd at 100 k$: the
# prudent year read 18.2 % there, 3.9 % once proportional (revue finance F11).
# Each line says where its rate comes from; NON VÉRIFIÉE where nothing was read.


@dataclass(frozen=True)
class Provision:
    name: str
    category: str  # attendu | plausible | queue
    base: str  # equity | debt | notional | fixed
    rate: float
    fixed_usd: float
    source: str


PROVISIONS = (
    Provision(
        "coûts d'exploitation",
        "attendu",
        "equity",
        0.03,
        275.0,
        "backtest FIDÈLE à 0,625, porte s0-f300 : 2,1 / 3,1 / 3,9 / 3,6 % de l'équité "
        "par an (2023-2026) ; fixe = ~260 traversées/an x 1 $ + gaz",
    ),
    Provision(
        "prime de la porte de régime",
        "attendu",
        "equity",
        0.015,
        0.0,
        "A/B du 2026-10-05 : s0-f300 contre porte OFF, -1,3 pt sur la période, -1,6 pt sur 12 mois",
    ),
    Provision(
        "pic de taux d'emprunt USDC",
        "plausible",
        "debt",
        0.56 * 7 / 365,
        0.0,
        "7 jours à 60 % APR au lieu de ~4 %",
    ),
    Provision(
        "glissement d'urgence P4",
        "queue",
        "equity",
        0.015,
        0.0,
        "150 bps x 2 épisodes sur la vente de P4 (formule N2)",
    ),
    Provision(
        "liquidation Aave",
        "queue",
        "debt",
        0.5 * 0.072,
        0.0,
        "close factor 50 % (NON VÉRIFIÉ) x pénalité 7,2 % (bonus 1,072 lu le 2026-10-05)",
    ),
    Provision(
        "liquidation Hyperliquid",
        "queue",
        "notional",
        0.025,
        0.0,
        "marge de maintenance perdue 2 % (mesurée sur notre compte, D2) + jambe nue "
        "jusqu'à P1 0,5 % (NON VÉRIFIÉE)",
    ),
    Provision(
        "divergence mark/oracle résiduelle",
        "queue",
        "notional",
        0.0025,
        0.0,
        "ordre de grandeur, NON VÉRIFIÉ",
    ),
    Provision(
        "refus du garde-fou à mi-cycle",
        "queue",
        "fixed",
        0.0,
        20.0,
        "une intervention manuelle, qui ne grossit pas avec le capital",
    ),
)

# The second size the classeur prints beside the config's: proportional lines
# only show their nature once two sizes sit side by side.
SECOND_CAPITAL = 100_000.0


@dataclass(frozen=True)
class Chassis:
    """A complete balance sheet, solved from the config."""

    capital: float
    cushion: float
    reserve: float
    target_ltv: float
    lt: float
    spot: float
    debt: float
    margin: float

    @property
    def collateral(self) -> float:
        return self.spot + self.cushion

    @property
    def observed_ltv(self) -> float:
        return self.debt / self.collateral

    @property
    def exposure(self) -> float:
        """Spot over equity, as README §4 defines it — not collateral over capital,
        which counted the cushion and read 2.14x for a built 2.09x (m3)."""
        return self.spot / self.capital

    def band(self, *, cushion_intact: bool = True) -> float:
        """Price fall that takes this sheet to the liquidation threshold."""
        cushion = self.cushion if cushion_intact else 0.0
        # Liquidation when debt = LT x spot x (1 - fall) + LT_USDC x cushion.
        return max(0.0, 1.0 - (self.debt - LT_USDC * cushion) / (self.lt * self.spot))

    @property
    def carry_gross(self) -> float:
        return FUNDING_APR * self.spot + STAKING_APR * self.spot - BORROW_APR * self.debt

    def scaled(self, capital: float) -> Chassis:
        """The same sheet at another capital: every leg is linear in it."""
        k = capital / self.capital
        return Chassis(
            capital=capital,
            cushion=self.cushion * k,
            reserve=self.reserve * k,
            target_ltv=self.target_ltv,
            lt=self.lt,
            spot=self.spot * k,
            debt=self.debt * k,
            margin=self.margin * k,
        )


def provision_usd(line: Provision, chassis: Chassis) -> float:
    """One provision on one sheet. The notional is the spot: the short matches it."""
    base = {
        "equity": chassis.capital,
        "debt": chassis.debt,
        "notional": chassis.spot,
        "fixed": 0.0,
    }[line.base]
    return line.rate * base + line.fixed_usd


# Same formula on both sides, so any gap beyond float noise is a real disagreement.
_MAX_DRIFT = 1e-9


def solve(config: Config, *, lt: float, target_ltv: float | None = None) -> Chassis:
    """Build the chassis the bot would actually hold, config in hand."""
    target_ltv = config.target_ltv if target_ltv is None else target_ltv
    capital = config.capital_usd
    cushion = capital * config.cushion_pct

    # Equity includes the cushion; only the rest is deployable (chantier 1.3).
    # Re-derived here rather than calling target_state so an alternative
    # target_ltv can be explored without mutating a frozen Config.
    leverage = config.short_leverage
    # The config's own coefficient when the target is the config's: the schema
    # accepts it within 1e-6 of the formula, and recomputing it here would make
    # the fixed-point check below fail on a config the bot loads.
    if target_ltv == config.target_ltv:
        exposure_mult = config.exposure_mult
    else:
        exposure_mult = 1.0 / (1.0 - target_ltv + 1.0 / leverage)

    # The HL reserve is capital held idle, exactly like the cushion, so it comes
    # out of what gets deployed. It is sized as a fraction of the notional,
    # which depends on the deployed amount, which depends on the reserve — so
    # solve the loop rather than treating it as free money:
    #     spot = m (capital - cushion - r-spot)  ->  spot = m(capital-cushion)/(1+mr)
    reserve_pct = config.emergency.hl_reserve_pct
    spot = exposure_mult * (capital - cushion) / (1.0 + exposure_mult * reserve_pct)
    return Chassis(
        capital=capital,
        cushion=cushion,
        reserve=spot * config.emergency.hl_reserve_pct,
        target_ltv=target_ltv,
        lt=lt,
        spot=spot,
        debt=target_ltv * spot,
        margin=spot * config.target_margin_ratio,
    )


def print_sheet(chassis: Chassis, config: Config) -> None:
    bands = derive_bands(chassis.lt, config)
    print(f"\n{'=' * 68}")
    print(f"  Bilan de référence — cible LTV {chassis.target_ltv:.3f}, LT {chassis.lt:.4f}")
    print("=" * 68)

    print(f"\n  {'capital':<28}{chassis.capital:>12,.0f} $")
    print(f"  {'dont coussin (Aave)':<28}{chassis.cushion:>12,.0f} $")
    print(f"  {'dont déployé':<28}{chassis.capital - chassis.cushion:>12,.0f} $")
    print(f"\n  {'spot wstETH':<28}{chassis.spot:>12,.0f} $")
    print(f"  {'dette USDC':<28}{chassis.debt:>12,.0f} $")
    print(f"  {'marge isolée HL':<28}{chassis.margin:>12,.0f} $")
    print(f"  {'réserve libre HL':<28}{chassis.reserve:>12,.0f} $")
    print(f"\n  {'exposition (spot / équité)':<28}{chassis.exposure:>12.2f} x")
    print(f"  {'LTV observé':<28}{chassis.observed_ltv:>12.4f}")
    print("     (sous la cible : le coussin compte comme collatéral)")

    print("\n  --- flanc bas : baisse de prix depuis la cible ---")
    print(f"  {'':<22}{'coussin plein':>15}{'parti, dette ±0':>17}{'après P3':>12}")
    for name, threshold in (
        ("P6 pompe", bands.ltv_pump),
        ("P3 / P4 coussin", bands.ltv_cushion),
    ):
        full = 1 - (chassis.debt / threshold - chassis.cushion) / chassis.spot
        gone = 1 - (chassis.debt / threshold) / chassis.spot
        spent = 1 - ((chassis.debt - chassis.cushion) / threshold) / chassis.spot
        print(f"  {name:<22}{-100 * full:>14.2f}%{-100 * gone:>16.2f}%{-100 * spent:>11.2f}%")
    # The liquidation weighs each collateral at its own threshold (m1).
    liq_full = chassis.band()
    liq_gone = chassis.band(cushion_intact=False)
    liq_spent = max(0.0, 1.0 - (chassis.debt - chassis.cushion) / (chassis.lt * chassis.spot))
    print(
        f"  {'LIQUIDATION':<22}{-100 * liq_full:>14.2f}%{-100 * liq_gone:>16.2f}%"
        f"{-100 * liq_spent:>11.2f}%"
    )

    print("\n  --- flanc haut : hausse de prix depuis la cible ---")
    m0 = config.target_margin_ratio
    reserve_ratio = chassis.reserve / chassis.spot
    for name, ratio in (
        ("I3 plancher de croisière", config.invariants.cruise_margin_floor),
        ("P5 pompe montante", config.emergency.margin_ratio_pump),
        ("P2 marge d'urgence", config.emergency.margin_ratio_reduce),
        ("LIQUIDATION", config.maintenance_margin),
    ):
        # A short at margin ratio m0 reaches ratio r at P/P0 = (1 + m0) / (1 + r).
        print(f"  {name:<28}{100 * ((1 + m0) / (1 + ratio) - 1):>11.2f}%")
    after = (1 + m0 + reserve_ratio) / (1 + config.maintenance_margin) - 1
    print(f"  {'LIQUIDATION après P2':<28}{100 * after:>11.2f}%   (réserve versée en marge)")
    print(f"  {'re-centrage haut':<28}{100 * config.recenter_up:>11.2f}%")

    print("\n  --- carry annuel (scénario, pas espérance) ---")
    funding = FUNDING_APR * chassis.spot
    staking = STAKING_APR * chassis.spot
    interest = BORROW_APR * chassis.debt
    print(f"  {'funding perçu':<28}{funding:>12,.0f} $")
    print(f"  {'staking':<28}{staking:>12,.0f} $")
    print(f"  {'intérêts Aave':<28}{-interest:>12,.0f} $")
    print(
        f"  {'BRUT':<28}{chassis.carry_gross:>12,.0f} $"
        f"   ({100 * chassis.carry_gross / chassis.capital:.1f} % du capital)"
    )

    print_provisions(chassis)


def print_provisions(chassis: Chassis) -> None:
    sheets = [chassis]
    if chassis.capital != SECOND_CAPITAL:
        sheets.append(chassis.scaled(SECOND_CAPITAL))
    heads = "".join(f"{f'{s.capital:,.0f} $':>14}" for s in sheets)
    print(f"\n  --- provisions (taux sur leur base, sources ci-dessous) ---\n  {'':<46}{heads}")
    for line in PROVISIONS:
        cells = "".join(f"{provision_usd(line, s):>14,.0f}" for s in sheets)
        print(f"  {line.name:<35}{line.category:>11}{cells}")

    print(f"\n  {'':<46}{heads}")
    rows = (
        ("BRUT", lambda s: s.carry_gross),
        ("= année typique", lambda s: s.carry_gross - _sum(s, ("attendu",))),
        ("= année prudente", lambda s: s.carry_gross - _sum(s, ("attendu", "plausible", "queue"))),
    )
    for label, value in rows:
        cells = "".join(
            f"{value(s):>9,.0f} {100 * value(s) / s.capital:>3.1f}%"[-14:].rjust(14) for s in sheets
        )
        print(f"  {label:<46}{cells}")
    print("\n  L'année prudente additionne tous les accidents de queue la même année.")
    print("  Sources :")
    for line in PROVISIONS:
        print(f"    - {line.name} : {line.source}")


def _sum(chassis: Chassis, categories: tuple[str, ...]) -> float:
    return sum(provision_usd(p, chassis) for p in PROVISIONS if p.category in categories)


def print_comparison(config: Config, lt: float) -> None:
    print(f"\n  Arbitrage de la cible LTV (LT {lt:.4f})\n")
    print(f"  {'cible':>7}{'expo':>7}{'bande':>10}{'coussin vide':>15}{'brut':>10}{'écart':>9}")
    base = solve(config, lt=lt, target_ltv=0.70).carry_gross
    for target in (0.70, 0.675, 0.65):
        c = solve(config, lt=lt, target_ltv=target)
        print(
            f"  {target:>7.3f}{c.exposure:>7.2f}{-100 * c.band():>9.2f}%"
            f"{-100 * c.band(cushion_intact=False):>14.2f}%"
            f"{c.carry_gross:>10,.0f}{c.carry_gross - base:>9,.0f}"
        )
    print("\n  La colonne « coussin vide » suppose le coussin parti sans que la dette")
    print("  baisse : c'est la lecture la plus prudente, celle sur laquelle décider.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO / "config.yaml")
    parser.add_argument("--lt", type=float, default=DEFAULT_LT)
    parser.add_argument("--target-ltv", type=float, default=None)
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.compare:
        print_comparison(config, args.lt)
        return

    chassis = solve(config, lt=args.lt, target_ltv=args.target_ltv)
    print_sheet(chassis, config)

    if args.target_ltv is not None and args.target_ltv != config.target_ltv:
        # Exploring a target the config does not hold: the bot's solver runs on
        # the config's target, so there is nothing to compare against.
        print("\n  point fixe non vérifié : cible explorée différente de celle de la config")
        return

    # A workbook that cannot contradict the code is the whole point; check it.
    solved = target_state(equity=chassis.capital, config=config, cushion_usd=chassis.cushion)
    drift = abs(solved.spot_target_usd - chassis.spot) / chassis.spot
    print(f"\n  point fixe contre target_state : écart {100 * drift:.4f} %")
    # Printing the gap was not enough: it read 7.06 % for days, because the
    # solver ignored the HL reserve, and nothing failed. A classeur that
    # disagrees with the code it describes must say so with its exit code.
    if drift > _MAX_DRIFT:
        raise SystemExit(
            f"ÉCHEC : le solveur du bot et ce classeur divergent de {100 * drift:.4f} % "
            "— l'un des deux est faux, ne rien décider sur ces chiffres."
        )


if __name__ == "__main__":
    main()
