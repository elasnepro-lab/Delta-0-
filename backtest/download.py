"""Peupler le cache d'archives Binance à la minute (chantier 7.2).

Trois séries sur cinq ans, soit ~170 archives mensuelles et ~300 Mo. La
commande est faite pour être relancée : un mois déjà pris ne repasse pas par
le réseau, donc une coupure — réseau, machine, Ctrl-C — ne coûte que les mois
qui manquaient encore.

    python -m backtest.download                          # 2021-01 -> dernier mois publié
    python -m backtest.download --series spot --from 2023-06
    python -m backtest.download --verify                 # compte les minutes manquantes

Deux absences n'ont pas le même sens et ne sortent pas le même code : les
derniers mois pas encore publiés (Binance publie avec quelques jours de retard)
sont normaux, un trou À L'INTÉRIEUR de la plage ne l'est pas — le backtest le
lirait comme un mois calme. Le second fait échouer la commande.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import httpx

from backtest import hl_funding
from backtest.binance import SERIES, ArchiveError, Funding, Series, missing_minutes
from backtest.cache import DEFAULT_ROOT, Month, Progress, ensure_range, read_month
from backtest.funding import gaps, intervals, read_funding_month

DEFAULT_START: Month = (2021, 1)
WORST_MONTHS_SHOWN = 5

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def label(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def human(size: int) -> str:
    return f"{size / 1e6:.1f} Mo"


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


def download(
    client: httpx.Client,
    root: Path,
    series: Sequence[Series],
    start: Month,
    end: Month,
    *,
    hyperliquid: bool = False,
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

    complete = True
    total_bytes = 0
    for one in series:
        print(f"=== {one.name} {label(start)} -> {label(end)}", file=out)
        report = ensure_range(client, root, one, start, end, progress=progress)
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
            verify(root, one, sorted(report.cached + report.downloaded), out)

    if hyperliquid and not fill_hyperliquid(
        client, root, start, end, verify_months=verify_months, progress=progress, out=out
    ):
        complete = False

    print(f"\n{human(total_bytes)} téléchargés, cache dans {root}", file=out)
    return EXIT_OK if complete else EXIT_INCOMPLETE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--series", choices=(*SERIES, hl_funding.NAME, "all"), default="all")
    parser.add_argument("--from", dest="start", type=parse_month, default=DEFAULT_START)
    parser.add_argument("--to", dest="end", type=parse_month, default=None)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--verify", action="store_true", help="relire et compter les trous")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    end: Month = args.end if args.end is not None else last_published_month()
    if args.start > end:
        print(f"plage vide : {label(args.start)} est après {label(end)}", file=sys.stderr)
        return EXIT_USAGE
    every = args.series == "all"
    wants_hl = every or args.series == hl_funding.NAME
    chosen = list(SERIES.values()) if every else [SERIES[s] for s in [args.series] if s in SERIES]

    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
            return download(
                client,
                args.root,
                chosen,
                args.start,
                end,
                hyperliquid=wants_hl,
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
