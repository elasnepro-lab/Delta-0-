"""Peupler le cache du backtest, toutes sources confondues (chantier 7.2).

Les archives Binance à la minute (~170 fichiers, ~300 Mo), le funding des deux
places, et les trois séries qui se lisent sur la chaîne : le taux de conversion
Lido, le coût de l'emprunt Aave, le prix de marché du stETH. La commande est
faite pour être relancée : ce qui est déjà pris ne repasse pas par le réseau,
donc une coupure — réseau, machine, Ctrl-C — ne coûte que ce qui manquait.

    python -m backtest.download                          # tout, 2021-01 -> dernier mois publié
    python -m backtest.download --series spot --from 2023-06
    python -m backtest.download --series aave --aave-reserve usdce-arbitrum
    python -m backtest.download --verify                 # compte les minutes manquantes

Les réserves Aave se collectent une par une, exprès : chacune coûte des dizaines
de minutes de RPC, et les anciennes (USDC.e, v2 mainnet) ne servent qu'aux
segments PROXY et INTERMÉDIAIRE.

Deux absences n'ont pas le même sens et ne sortent pas le même code : les
derniers mois pas encore publiés (Binance publie avec quelques jours de retard)
sont normaux, un trou À L'INTÉRIEUR de la plage ne l'est pas — le backtest le
lirait comme un mois calme. Le second fait échouer la commande.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import httpx

from backtest import aave_rates, hl_funding, lido, steth
from backtest.binance import SERIES, ArchiveError, Funding, Series, missing_minutes
from backtest.cache import Month, Progress, ensure_range, read_month
from backtest.chain import Chain
from backtest.funding import gaps, intervals, read_funding_month

DEFAULT_START: Month = (2021, 1)
WORST_MONTHS_SHOWN = 5

# La racine du cache, pas celle des archives : les archives Binance vivent dans
# `<racine>/binance`, et chaque source de chaîne a son propre dossier à côté.
# Les confondre ferait écrire les séries on-chain sous `binance/`.
CACHE_ROOT = Path("data") / "backtest"
ARCHIVES = "binance"

# Les sources qui ne sont pas des archives Binance. Chacune a son rythme et son
# cache ; « all » les prend toutes.
LIDO = "lido"
AAVE = "aave"
STETH = "steth"
EXTRAS = (hl_funding.NAME, LIDO, AAVE, STETH)

# La réserve du montage. Les autres (USDC.e, v2 mainnet) servent aux segments
# anciens et se collectent exprès : elles coûtent des heures de RPC chacune.
DEFAULT_RESERVE = "usdc-arbitrum"

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def label(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def human(size: int) -> str:
    return f"{size / 1e6:.1f} Mo"


def stamp(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


def month_first_day(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}-01"


def month_last_day(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}-{calendar.monthrange(*month)[1]:02d}"


def parse_month(text: str) -> Month:
    try:
        moment = dt.datetime.strptime(text, "%Y-%m").replace(tzinfo=dt.UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"mois attendu au format AAAA-MM, reçu {text!r}") from None
    return moment.year, moment.month


def last_published_month(today: dt.date | None = None) -> Month:
    """Le mois précédent : l'archive du mois courant n'existe pas encore."""
    day = today if today is not None else dt.datetime.now(dt.UTC).date()
    previous = day.replace(day=1) - dt.timedelta(days=1)
    return previous.year, previous.month


def verify_funding(root: Path, taken: Sequence[Month], out: TextIO) -> None:
    """Décrire la série de funding : combien de versements, sur quelles périodes, et les trous."""
    rows: list[Funding] = []
    for year, month in taken:
        rows.extend(read_funding_month(root, year, month))
    describe_funding(rows, out)


def verify(root: Path, series: Series, taken: Sequence[Month], out: TextIO) -> None:
    """Relire chaque mois pris et compter ce qui manque dedans.

    Informatif, jamais bloquant : une minute absente peut être une vraie panne
    de la place, que le backtest doit voir plutôt que subir. Ce qui bloque,
    c'est un MOIS manquant au milieu de la plage.
    """
    if not series.is_candles:
        verify_funding(root, taken, out)
        return
    holes = {}
    for year, month in taken:
        gap = missing_minutes(read_month(root, series, year, month), year, month)
        if gap:
            holes[(year, month)] = gap
    if not holes:
        print(f"  vérification : {len(taken)} mois relus, aucune minute manquante", file=out)
        return
    worst = sorted(holes.items(), key=lambda item: item[1], reverse=True)[:WORST_MONTHS_SHOWN]
    detail = ", ".join(f"{label(month)} ({gap})" for month, gap in worst)
    print(
        f"  vérification : {sum(holes.values())} minute(s) manquante(s) sur {len(holes)} mois"
        f" — les plus creux : {detail}",
        file=out,
    )


def describe_funding(rows: Sequence[Funding], out: TextIO) -> None:
    """Combien de versements, sur quelles périodes, et les trous. Décrit, ne corrige pas."""
    periods = ", ".join(f"{hours} h x {count}" for hours, count in sorted(intervals(rows).items()))
    holes = gaps(rows)
    manque = (
        "aucun trou" if not holes else f"{len(holes)} trou(s), {sum(h for _, h in holes):.0f} h"
    )
    print(f"  vérification : {len(rows)} versements, périodes {periods}, {manque}", file=out)


def fill_hyperliquid(
    client: httpx.Client,
    root: Path,
    start: Month,
    end: Month,
    *,
    verify_months: bool,
    progress: Progress,
    out: TextIO,
) -> bool:
    """Le funding HL, qui n'a pas d'archive. Rend False si la plage a une anomalie."""
    first = max(start, hl_funding.FIRST_MONTH)
    print(f"=== {hl_funding.NAME} {label(first)} -> {label(end)}", file=out)
    if first > end:
        print("  hors plage : Hyperliquid ne cote pas l'ETH avant 2023-05", file=out)
        return True

    report = hl_funding.fill_range(client, root, first, end, progress=progress)
    print(
        f"  {len(report.fetched)} mois pris, {len(report.cached)} déjà en cache,"
        f" {len(report.rows)} versements",
        file=out,
    )
    if report.not_final:
        running = ", ".join(label(month) for month in report.not_final)
        print(f"  mois en cours, servi mais pas mis en cache : {running}", file=out)
    if verify_months:
        describe_funding(hl_funding.as_funding(report.rows), out)
    if report.empty_unexpected:
        vides = ", ".join(label(month) for month in report.empty_unexpected)
        print(f"  VIDE alors que la place cotait : {vides}", file=out)
        return False
    return True


def fill_lido(client: httpx.Client, root: Path, start: Month, end: Month, out: TextIO) -> bool:
    """Le taux de conversion wstETH/stETH, un point par jour à minuit UTC."""
    first = max(lido.FIRST_DAY, month_first_day(start))
    last = month_last_day(end)
    print(f"=== {LIDO} {first} -> {last}", file=out)
    if first > last:
        print(f"  hors plage : rien de lisible avant le {lido.FIRST_DAY}", file=out)
        return True

    lus: list[str] = []
    points = lido.ensure_days(
        Chain("mainnet", client, root=root / "chain"),
        root,
        first,
        last,
        progress=lambda state, day: lus.append(day) if state == "fetched" else None,
    )
    print(
        f"  {len(points)} jours ({len(lus)} lus), ratio"
        f" {points[0].ratio:.6f} -> {points[-1].ratio:.6f}",
        file=out,
    )
    chutes = lido.drops(points)
    if chutes:
        jour, ampleur = min(chutes, key=lambda chute: chute[1])
        print(
            f"  {len(chutes)} BAISSE(S) du ratio, la pire {ampleur * 100:.4f} % le {jour}"
            " — slashing Lido, que la jambe short ne couvre pas",
            file=out,
        )
    return True


def fill_aave(
    client: httpx.Client,
    root: Path,
    reserve: aave_rates.Reserve,
    start: Month,
    end: Month,
    *,
    step_s: int,
    progress: Progress,
    out: TextIO,
) -> bool:
    """Le coût de l'emprunt, relevé par pas fixe dans les événements de la réserve."""
    first = max(start, aave_rates.listing_month(reserve))
    print(f"=== {AAVE}:{reserve.name} {label(first)} -> {label(end)} (pas {step_s} s)", file=out)
    if first > end:
        print(f"  hors plage : listée seulement depuis le {reserve.listed_on}", file=out)
        return True

    report = aave_rates.ensure_range(
        Chain(reserve.chain, client, root=root / "chain"),
        root,
        reserve,
        first,
        end,
        step_s=step_s,
        progress=progress,
    )
    print(
        f"  {len(report.fetched)} mois pris, {len(report.cached)} déjà en cache,"
        f" {len(report.marks)} relevés",
        file=out,
    )
    if report.marks:
        print(f"  retard maximal d'un relevé : {report.worst_stale_blocks} blocs", file=out)
    if report.missing_hours:
        print(f"  {len(report.missing_hours)} repère(s) sans aucun événement", file=out)
    return True


def fill_steth(client: httpx.Client, root: Path, out: TextIO) -> bool:
    """Le prix de marché du stETH : celui de la sortie, pas celui du danger."""
    print(f"=== {STETH} (Chainlink stETH/ETH, les deux phases)", file=out)
    chain = Chain("mainnet", client, root=root / "chain")
    for phase in (1, 2):
        rounds = steth.ensure_phase(chain, root, phase)
        if rounds:
            print(
                f"  phase {phase} : {len(rounds)} rounds,"
                f" {stamp(rounds[0].ts)} -> {stamp(rounds[-1].ts)}",
                file=out,
            )
    joined = steth.all_rounds(root)
    if joined:
        creux = min(joined, key=lambda entry: entry.raw)
        print(f"  plus bas publié : {creux.ratio:.5f} le {stamp(creux.ts)}", file=out)
    print("  un point par jour : le creux intrajournalier n'y figure pas", file=out)
    return True


def download(
    client: httpx.Client,
    root: Path,
    series: Sequence[Series],
    start: Month,
    end: Month,
    *,
    extras: Sequence[str] = (),
    reserve: aave_rates.Reserve | None = None,
    step_s: int = aave_rates.DAY_S,
    verify_months: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """Remplir le cache pour chaque série, et dire ce qui manque encore."""

    # Les deux sources ne nomment pas leurs états pareil : les archives disent
    # downloaded/missing, Hyperliquid fetched/partial. Un mois pris ne doit pas
    # s'afficher « absent » parce qu'il vient de l'autre source.
    said = {"downloaded": "pris", "fetched": "pris", "partial": "en cours", "missing": "absent"}

    def progress(state: str, year: int, month: int) -> None:
        if state != "cached":
            print(f"    {said.get(state, state)} {year:04d}-{month:02d}", file=out)

    archives = root / ARCHIVES
    complete = True
    total_bytes = 0
    for one in series:
        print(f"=== {one.name} {label(start)} -> {label(end)}", file=out)
        report = ensure_range(client, archives, one, start, end, progress=progress)
        total_bytes += report.bytes_downloaded
        print(
            f"  {len(report.downloaded)} pris ({human(report.bytes_downloaded)}),"
            f" {len(report.cached)} déjà en cache, {len(report.missing)} absent(s)",
            file=out,
        )
        if report.interior_gaps:
            trous = ", ".join(label(month) for month in report.interior_gaps)
            print(
                f"  TROU dans la plage : {trous} — ce n'est pas une publication en retard",
                file=out,
            )
            complete = False
        if verify_months:
            verify(archives, one, sorted(report.cached + report.downloaded), out)

    if hl_funding.NAME in extras and not fill_hyperliquid(
        client, root, start, end, verify_months=verify_months, progress=progress, out=out
    ):
        complete = False
    if LIDO in extras and not fill_lido(client, root, start, end, out):
        complete = False
    if AAVE in extras and not fill_aave(
        client,
        root,
        reserve if reserve is not None else aave_rates.RESERVES[DEFAULT_RESERVE],
        start,
        end,
        step_s=step_s,
        progress=progress,
        out=out,
    ):
        complete = False
    if STETH in extras and not fill_steth(client, root, out):
        complete = False

    if series:
        print(f"\n{human(total_bytes)} d'archives téléchargées", file=out)
    print(f"cache dans {root}", file=out)
    return EXIT_OK if complete else EXIT_INCOMPLETE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--series", choices=(*SERIES, *EXTRAS, "all"), default="all")
    parser.add_argument(
        "--aave-reserve", choices=tuple(aave_rates.RESERVES), default=DEFAULT_RESERVE
    )
    parser.add_argument(
        "--step",
        type=int,
        default=aave_rates.DAY_S,
        help="pas des relevés Aave, en secondes (défaut : un jour)",
    )
    parser.add_argument("--from", dest="start", type=parse_month, default=DEFAULT_START)
    parser.add_argument("--to", dest="end", type=parse_month, default=None)
    parser.add_argument("--root", type=Path, default=CACHE_ROOT)
    parser.add_argument("--verify", action="store_true", help="relire et compter les trous")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    end: Month = args.end if args.end is not None else last_published_month()
    if args.start > end:
        print(f"plage vide : {label(args.start)} est après {label(end)}", file=sys.stderr)
        return EXIT_USAGE
    every = args.series == "all"
    extras = list(EXTRAS) if every else [args.series] if args.series in EXTRAS else []
    chosen = list(SERIES.values()) if every else [SERIES[s] for s in [args.series] if s in SERIES]

    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
            return download(
                client,
                args.root,
                chosen,
                args.start,
                end,
                extras=extras,
                reserve=aave_rates.RESERVES[args.aave_reserve],
                step_s=args.step,
                verify_months=args.verify,
            )
    except KeyboardInterrupt:
        print("\ninterrompu — relancer reprend où on s'est arrêté", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (httpx.HTTPError, ArchiveError, hl_funding.HlError) as e:
        print(f"échec : {e}\nrelancer reprend où on s'est arrêté", file=sys.stderr)
        return EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
