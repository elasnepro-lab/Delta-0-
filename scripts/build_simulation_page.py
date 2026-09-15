"""Render the five-year projections as a standalone page.

Regenerate whenever a parameter moves — that is the point of having it as a
script rather than a spreadsheet:

    uv run python scripts/build_simulation_page.py -o simulations.html
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import simulate as sim  # noqa: E402
from classeur import BORROW_APR, FUNDING_APR, STAKING_APR, solve  # noqa: E402

from delta0.config import Config, load_config  # noqa: E402

Writer = Callable[[str], None]
CSS = Path(__file__).with_name("simulation_page.css").read_text(encoding="utf-8")
FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&"
    "family=IBM+Plex+Sans:wght@400;500;600&"
    "family=IBM+Plex+Mono:wght@400;500&display=swap"
)


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def masthead(w: Writer) -> None:
    w('<header class="mast">')
    w('<p class="eyebrow">Delta-0 - chantier 1.5 - genere depuis config.yaml</p>')
    w("<h1>Cinq ans, quatre capitaux, trois cibles</h1>")
    w(
        '<p class="standfirst">Projection du chassis sur cinq annees, profits '
        "recomposes. La chaine demandee : <strong>rendement brut, frais de "
        "transaction, net, provision de liquidation, net final</strong>. Un "
        "scenario, pas une prevision : les hypotheses sont listees et "
        "discutables, et la page se regenere par "
        '<span class="m">scripts/build_simulation_page.py</span> des qu\'un '
        "parametre bouge.</p>"
    )
    w("</header>")


def headline(w: Writer, args: argparse.Namespace) -> None:
    w("<section>")
    w("<h2>Le resultat qui decide</h2>")
    w(
        '<p class="sub">Cumul du net final sur cinq ans, et capital atteint. '
        "Une seule ligne a retenir : la cible la plus agressive n'est pas la "
        "plus rentable.</p>"
    )
    w('<div class="scroll"><table><thead><tr><th>Capital initial</th>')
    for t in sim.TARGETS:
        w(f'<th class="num">cible {t:.3f}</th>')
    w("</tr></thead><tbody>")

    for cap in sim.CAPITALS:
        totals: dict[float, float] = {}
        finals: dict[float, float] = {}
        for t in sim.TARGETS:
            rows = sim.run_scenario(args.config, cap, t)
            totals[t] = sum(r.net_final for r in rows)
            finals[t] = rows[-1].capital + rows[-1].net_final
        best = max(totals, key=lambda k: totals[k])
        w(f"<tr><td>{money(cap)} $</td>")
        for t in sim.TARGETS:
            cls = ' class="num win"' if t == best else ' class="num"'
            w(
                f"<td{cls}>{money(totals[t])} $<br>"
                f'<span class="soft">vers {money(finals[t])} $</span></td>'
            )
        w("</tr>")
    w("</tbody></table></div>")

    w('<div class="note good"><h4>La securite paie, dans ce modele</h4>')
    w(
        "<p>Descendre la cible de 0,700 a 0,675 coute du carry brut et en rend "
        "davantage : la provision de liquidation baisse plus vite que le "
        "rendement. A 0,650 le gain supplementaire devient marginal, ce qui "
        "fait de <strong>0,675 le point d'equilibre</strong> : plus "
        "d'exposition qu'a 0,650 pour un net equivalent.</p>"
    )
    w(
        "<p>Ce resultat tient entierement au modele de frequence de liquidation. "
        "Voir la section de sensibilite avant d'en tirer une conclusion "
        "ferme.</p></div>"
    )
    w("</section>")


def allocation(w: Writer, config: Config) -> None:
    """Where the capital actually sits — the first question a reader has."""
    w("<section>")
    w("<h2>Ou va le capital</h2>")
    w(
        '<p class="sub">Le capital n\'est jamais ailleurs : il se repartit entre '
        "quatre postes qui bouclent au dollar pres. Les proportions ne dependent "
        "pas du montant, le chassis etant lineaire.</p>"
    )
    w(
        '<div class="scroll"><table><thead><tr><th>Capital</th>'
        "<th>Coussin (Aave)</th><th>Reserve (HL)</th>"
        "<th>Fonds propres Aave</th><th>Marge isolee (HL)</th>"
        "<th>Notionnel</th></tr></thead><tbody>"
    )
    for cap in sim.CAPITALS:
        chassis = solve(config.model_copy(update={"capital_usd": cap}), lt=sim.LT)
        own = chassis.spot - chassis.debt
        w(
            f"<tr><td>{money(cap)} $</td>"
            f'<td class="num">{money(chassis.cushion)}'
            f'<br><span class="soft">{100 * chassis.cushion / cap:.1f} %</span></td>'
            f'<td class="num">{money(chassis.reserve)}'
            f'<br><span class="soft">{100 * chassis.reserve / cap:.1f} %</span></td>'
            f'<td class="num">{money(own)}'
            f'<br><span class="soft">{100 * own / cap:.1f} %</span></td>'
            f'<td class="num">{money(chassis.margin)}'
            f'<br><span class="soft">{100 * chassis.margin / cap:.1f} %</span></td>'
            f'<td class="num">{money(chassis.spot)}'
            f'<br><span class="soft">{chassis.spot / cap:.2f} x</span></td></tr>'
        )
    w("</tbody></table></div>")

    ref = solve(config.model_copy(update={"capital_usd": 20_000.0}), lt=sim.LT)
    idle = (ref.cushion + ref.reserve + ref.margin) / 20_000.0
    reserve_share = ref.reserve / 20_000.0
    w('<div class="note"><h4>Deux lectures qui surprennent</h4>')
    w(
        f"<p>La reserve Hyperliquid pese <strong>{100 * reserve_share:.1f} % du "
        f"capital</strong>, pas les {100 * config.emergency.hl_reserve_pct:.0f} % "
        "annonces : ce pourcentage-la porte sur le <em>notionnel</em>, qui vaut "
        "plus de deux fois le capital. Son cout de portage est donc plus lourd "
        "qu'il n'y parait.</p>"
    )
    w(
        f"<p>Et <strong>{100 * idle:.0f} % du capital ne travaille pas dans la "
        "boucle</strong> : coussin, reserve et marge isolee ne produisent ni "
        "funding ni staking. C'est le prix structurel de la neutralite — la "
        "marge isolee est ce qui permet d'avoir un short, et les deux autres "
        "postes sont ce qui permet d'y survivre.</p></div>"
    )
    w("</section>")


def yearly(w: Writer, args: argparse.Namespace) -> None:
    w("<section>")
    w("<h2>Annee par annee</h2>")
    w(
        '<p class="sub">Profits recomposes dans le capital de l\'annee suivante, '
        "conformement a la politique d'ecremage v1. Le rendement en pourcentage "
        "est constant par construction : le chassis est lineaire en capital.</p>"
    )
    for cap in sim.CAPITALS:
        w(f"<h3>Capital initial {money(cap)} $</h3>")
        w(
            '<div class="scroll"><table><thead><tr>'
            "<th>Cible / an</th><th>Capital</th><th>Brut</th><th>Frais</th>"
            "<th>Net</th><th>Prov. liq.</th><th>Net final</th><th>Rdt</th>"
            "</tr></thead><tbody>"
        )
        for t in sim.TARGETS:
            rows = sim.run_scenario(args.config, cap, t)
            band = -100 * rows[0].band_empty
            years = 1 / sim.liquidation_frequency(rows[0].band_empty)
            w(
                f'<tr class="grouphead"><td colspan="8">cible {t:.3f} - bande '
                f"coussin vide {band:.2f} % - une liquidation attendue tous les "
                f"{years:.1f} ans</td></tr>"
            )
            for r in rows:
                w(
                    f"<tr><td>an {r.year}</td>"
                    f'<td class="num">{money(r.capital)}</td>'
                    f'<td class="num">{money(r.gross)}</td>'
                    f'<td class="num soft">-{money(r.fees)}</td>'
                    f'<td class="num">{money(r.net)}</td>'
                    f'<td class="num bad">-{money(r.liq_provision)}</td>'
                    f'<td class="num"><b>{money(r.net_final)}</b></td>'
                    f'<td class="num">{r.yield_pct:.1f} %</td></tr>'
                )
            total = sum(r.net_final for r in rows)
            w(
                f'<tr class="total"><td>cumul 5 ans</td><td colspan="5"></td>'
                f'<td class="num">{money(total)}</td><td></td></tr>'
            )
        w("</tbody></table></div>")
    w("</section>")


def sensitivity(w: Writer, args: argparse.Namespace) -> None:
    w("<section>")
    w("<h2>Ce dont tout depend</h2>")
    w(
        '<p class="sub">La frequence de liquidation est le parametre le plus '
        "discutable de cette page, et celui qui decide du classement. Voici le "
        "cumul cinq ans sur 20 000 $ pour plusieurs hypotheses.</p>"
    )
    base = sim.LIQ_FREQ_AT_10PCT
    w('<div class="scroll"><table><thead><tr>')
    w("<th>Frequence d'une chute de 10 %</th>")
    for t in sim.TARGETS:
        w(f'<th class="num">{t:.3f}</th>')
    w("<th>Gagnant</th></tr></thead><tbody>")
    for freq in (0.0, 0.15, 0.3, 0.6, 1.2, 2.4):
        sim.LIQ_FREQ_AT_10PCT = freq
        tot = {
            t: sum(r.net_final for r in sim.run_scenario(args.config, 20_000.0, t))
            for t in sim.TARGETS
        }
        best = max(tot, key=lambda k: tot[k])
        label = "jamais" if freq == 0 else f"{freq:.2f} / an"
        w(f"<tr><td>{label}</td>")
        for t in sim.TARGETS:
            cls = ' class="num win"' if t == best else ' class="num"'
            w(f"<td{cls}>{money(tot[t])}</td>")
        w(f'<td><span class="pill">{best:.3f}</span></td></tr>')
    sim.LIQ_FREQ_AT_10PCT = base
    w("</tbody></table></div>")

    w('<div class="note crit">')
    w("<h4>Le basculement se joue entre 0,3 et 0,6 par an</h4>")
    w(
        "<p>Si une chute de 10 % assez rapide pour battre les defenses arrive "
        "moins de trois fois par decennie, la cible agressive redevient la plus "
        "rentable. Au-dela, la prudence paie. <strong>Nous ne savons pas de quel "
        "cote nous sommes</strong> : c'est ce que le backtest de M2b doit "
        "mesurer, et d'ici la ce choix repose sur une hypothese, pas sur une "
        "observation.</p>"
    )
    w(
        "<p>Le modele retenu ici donne une liquidation tous les 1,3 an a la cible "
        "0,700, ce qui est probablement pessimiste. A l'inverse, il ignore que "
        "plusieurs accidents peuvent se cumuler dans une meme annee.</p></div>"
    )
    w("</section>")


def assumptions(w: Writer, config: Config) -> None:
    w("<section>")
    w("<h2>Les hypotheses, en toutes lettres</h2>")
    w(
        '<p class="sub">Chacune est une constante nommee dans '
        '<span class="m">scripts/simulate.py</span> : changez-la, relancez, la '
        "page suit.</p>"
    )
    cells = (
        (
            "Revenus",
            f"Funding <b>{100 * FUNDING_APR:.0f} %</b>, staking "
            f"<b>{100 * STAKING_APR:.1f} %</b>, emprunt "
            f"<b>{100 * BORROW_APR:.0f} %</b>. Le funding est un scenario : il a "
            "ete bien plus bas sur de longues periodes de 2023-2024.",
        ),
        (
            "Frais de transaction",
            f"{sim.RECENTRES_PER_YEAR:.0f} re-centrages a "
            f"{sim.COST_PER_RECENTRE:.0f} $, {sim.TRUEUPS_PER_YEAR:.1f} "
            f"re-truages, {sim.SKIMS_PER_YEAR:.0f} ecremages a "
            f"{sim.COST_PER_SKIM:.0f} $, plus {sim.GAS_PER_YEAR:.0f} $ de gaz. "
            "Tout est lineaire en notionnel, ce qui flatte les petits chassis : "
            "le dollar de pont et le gaz ne retrecissent pas.",
        ),
        (
            "Liquidation",
            f"Cout d'une liquidation propre : <b>{100 * sim.LIQ_COST_PER_DEBT:.1f} % "
            "de la dette</b>, facteur de fermeture 50 % a 7,2 % de penalite. "
            f"Frequence : loi de puissance d'exposant {sim.LIQ_DECAY}, calee sur "
            f"{sim.LIQ_FREQ_AT_10PCT} chute de 10 % par an, et seules comptent "
            "les chutes assez rapides pour battre les defenses.",
        ),
        (
            "Chassis",
            f"Seuil de liquidation Aave <b>{sim.LT}</b> lu on-chain, coussin "
            f"{100 * config.cushion_pct:.0f} % du capital, reserve Hyperliquid "
            f"{100 * config.emergency.hl_reserve_pct:.0f} % du notionnel, levier "
            f"short {config.short_leverage}x. Coussin et reserve sortent du "
            "capital deploye.",
        ),
    )
    w('<div class="grid two">')
    for title, body in cells:
        w(f'<div class="cell"><dt>{title}</dt><dd>{body}</dd></div>')
    w("</div>")

    w('<div class="note"><h4>Ce qui n\'est pas modelise</h4>')
    w(
        "<p>Les <strong>episodes de funding negatif</strong> : la porte de regime "
        "en laisse passer trois a cinq semaines a chaque retournement de cycle, "
        "de l'ordre de 1 000 $ par episode, et cela n'apparait nulle part "
        "ci-dessus. Les <strong>pics de taux d'emprunt USDC</strong>. Le "
        "<strong>depeg stETH</strong>, qui consomme la bande sans que l'ETH "
        "bouge. La <strong>fiscalite</strong>. Et le risque de contrat sur six "
        "dependances.</p>"
    )
    w(
        "<p>Autrement dit : la colonne net final est un plafond, pas une "
        "esperance. Une annee ordinaire y retranche encore les couts "
        "attendus.</p></div>"
    )
    w("</section>")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=REPO / "config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    out: list[str] = []
    w = out.append

    w("<title>Simulations Delta-0 sur cinq ans</title>")
    w('<link rel="preconnect" href="https://fonts.googleapis.com">')
    w('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
    w(f'<link rel="stylesheet" href="{FONTS}">')
    w(f"<style>{CSS}</style>")
    w('<div class="wrap">')

    masthead(w)
    headline(w, args)
    allocation(w, config)
    yearly(w, args)
    sensitivity(w, args)
    assumptions(w, config)

    w(
        "<footer><p>Genere depuis <span class='m'>config.yaml</span> par "
        "<span class='m'>scripts/build_simulation_page.py</span>. Les parametres "
        "du chassis ne sont pas figes : relancer le script apres toute "
        "modification met cette page a jour. Le seuil de liquidation Aave se "
        "relit avec <span class='m'>scripts/read_aave_params.py</span>.</p></footer>"
    )
    w("</div>")

    args.out.write_text("\n".join(out), encoding="utf-8")
    print(f"page ecrite : {args.out} ({args.out.stat().st_size / 1024:.0f} Ko)")


if __name__ == "__main__":
    main()
