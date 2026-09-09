"""Generate the reference balance sheet from the config — the Model C workbook.

A workbook maintained beside the code is how a liquidation threshold of 0.81
survived for months while Arbitrum applied 0.79: the two never had to agree.
This derives every figure from `config.yaml` and the on-chain threshold, so a
divergence becomes impossible rather than merely unlikely.

    uv run python scripts/classeur.py                 # current config
    uv run python scripts/classeur.py --compare       # candidate target LTVs
    uv run python scripts/classeur.py --lt 0.75       # a governance cut

The bands are printed twice on purpose. The nominal one assumes the cushion is
intact; the second assumes P3 has spent it, which is the situation that holds
immediately after the cushion did its job. The second is the one to plan on.
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

# Carry assumptions. These are a SCENARIO, not an expectation: funding averaged
# far less than 11 % over long stretches of 2023-2024 (audit F10). The régime
# report of M2b is what should replace them.
FUNDING_APR = 0.11
STAKING_APR = 0.027
BORROW_APR = 0.05

# Section H of the audit, split by nature rather than lumped. "Expected" lines
# are what a normal year costs; only the tail is genuinely optional.
PROVISIONS = (
    ("carry négatif avant PARKED + whipsaw", 1_000.0, "attendu"),
    ("frais de re-centrage hors budget", 100.0, "attendu"),
    ("pompes dues au funding négatif", 100.0, "attendu"),
    ("pic de taux d'emprunt USDC", 400.0, "plausible"),
    ("slippage d'urgence P4", 300.0, "queue"),
    ("liquidation Aave « propre »", 1_300.0, "queue"),
    ("divergence mark/oracle résiduelle", 100.0, "queue"),
    ("refus du garde-fou à mi-cycle", 20.0, "queue"),
)


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
        return self.collateral / self.capital

    def band(self, *, cushion_intact: bool = True) -> float:
        """Price fall that takes this sheet to the liquidation threshold."""
        cushion = self.cushion if cushion_intact else 0.0
        return max(0.0, 1.0 - (self.debt / self.lt - cushion) / self.spot)

    @property
    def carry_gross(self) -> float:
        return FUNDING_APR * self.spot + STAKING_APR * self.spot - BORROW_APR * self.debt


def solve(config: Config, *, lt: float, target_ltv: float | None = None) -> Chassis:
    """Build the chassis the bot would actually hold, config in hand."""
    target_ltv = config.target_ltv if target_ltv is None else target_ltv
    capital = config.capital_usd
    cushion = capital * config.cushion_pct

    # Equity includes the cushion; only the rest is deployable (chantier 1.3).
    # Re-derived here rather than calling target_state so an alternative
    # target_ltv can be explored without mutating a frozen Config.
    leverage = config.short_leverage
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
    print(f"\n  {'exposition':<28}{chassis.exposure:>12.2f} x")
    print(f"  {'LTV observé':<28}{chassis.observed_ltv:>12.4f}")
    print("     (sous la cible : le coussin compte comme collatéral)")

    print("\n  --- bandes de sécurité ---")
    print(f"  {'':<28}{'coussin plein':>16}{'coussin vide':>16}")
    for name, threshold in (
        ("P6 pompe", bands.ltv_pump),
        ("P3 coussin", bands.ltv_cushion),
        ("P4 désendettement", bands.ltv_deleverage),
        ("LIQUIDATION", chassis.lt),
    ):
        full = 1 - (chassis.debt / threshold - chassis.cushion) / chassis.spot
        empty = 1 - (chassis.debt / threshold) / chassis.spot
        print(f"  {name:<28}{-100 * full:>15.2f}%{-100 * empty:>15.2f}%")

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

    expected = sum(a for _, a, n in PROVISIONS if n == "attendu")
    total = sum(a for _, a, _ in PROVISIONS)
    print(f"\n  {'- coûts attendus':<28}{-expected:>12,.0f} $")
    print(
        f"  {'= année typique':<28}{chassis.carry_gross - expected:>12,.0f} $"
        f"   ({100 * (chassis.carry_gross - expected) / chassis.capital:.1f} %)"
    )
    print(
        f"  {'= année prudente':<28}{chassis.carry_gross - total:>12,.0f} $"
        f"   ({100 * (chassis.carry_gross - total) / chassis.capital:.1f} %)"
    )


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
    print("\n  La bande « coussin vide » est celle qui vaut juste après que P3")
    print("  a fait son travail. C'est sur elle qu'il faut décider.")


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

    # A workbook that cannot contradict the code is the whole point; check it.
    solved = target_state(equity=chassis.capital, config=config, cushion_usd=chassis.cushion)
    drift = abs(solved.spot_target_usd - chassis.spot) / chassis.spot
    print(f"\n  point fixe contre target_state : écart {100 * drift:.4f} %")


if __name__ == "__main__":
    main()
