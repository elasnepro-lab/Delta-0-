"""Le funding Hyperliquid : la seule série du backtest qui n'a aucune archive.

Elle se pagine depuis l'API publique (`POST /info`, `fundingHistory`), 500
lignes par réponse, et se range dans un cache à elle : du JSON par mois, les
lignes gardées **telles que l'API les a rendues** pour qu'un mois du cache
puisse se confronter à la source des années plus tard.

Trois vérités lues le 2026-09-16, pas rappelées de mémoire :

- la première ligne servie est le **2023-05-12** ; avant, Hyperliquid ne cotait
  pas l'ETH et le backtest n'a qu'un proxy Binance ;
- le rythme change le **2023-06-08** : toutes les 8 h avant, horaire après ;
- les horodatages dérivent de quelques millisecondes (…420, …037).

Ce que ce module ne fait pas : décider ce que vaut un taux de la phase 8 h.
L'API ne dit nulle part si ce taux est servi **par 8 h ou par heure**, et les
deux lectures diffèrent d'un facteur 8 sur le coût du portage. C'est NON
VÉRIFIÉ (docs/backtest/inventaire-prix-funding.md), ça tombe dans le segment
INTERMÉDIAIRE, et ça se tranche au gel de la méthode — pas dans un défaut
silencieux au fond d'un module.

Un mois n'est mis en cache que **lorsqu'il est terminé** : le mois courant se
remplit encore, et le figer donnerait une série qui paraît complète.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from backtest.binance import Funding, month_bounds_ms, months
from backtest.cache import CacheMissError, Month, Progress

NAME = "hl-funding"
INFO_URL = "https://api.hyperliquid.xyz/info"
COIN = "ETH"

PAGE_LIMIT = 500  # mesuré : une réponse ne dépasse jamais 500 lignes
FIRST_MONTH: Month = (2023, 5)  # première ligne servie : 2023-05-12
HOURLY_SINCE_MS = int(dt.datetime(2023, 6, 8, tzinfo=dt.UTC).timestamp() * 1000)
EIGHT_HOURS = 8

_PARTIAL_SUFFIX = ".partial"


class HlError(Exception):
    """Ce que la place a rendu ne peut pas être pris pour du funding."""


@dataclass(frozen=True, slots=True)
class HlFunding:
    ts_ms: int
    rate: float  # sur la période servie, dont l'API ne dit rien
    premium: float


@dataclass(slots=True)
class HlReport:
    """Ce qu'une plage a coûté, et ce qui mérite un œil."""

    fetched: list[Month] = field(default_factory=list)
    cached: list[Month] = field(default_factory=list)
    not_final: list[Month] = field(default_factory=list)
    empty_unexpected: list[Month] = field(default_factory=list)
    rows: list[HlFunding] = field(default_factory=list)


def month_file(root: Path, year: int, month: int) -> Path:
    return root / "hyperliquid" / "funding" / f"{COIN}-{year:04d}-{month:02d}.json"


def declared_period_hours(ts_ms: int) -> int:
    """La période d'un versement, par RÈGLE datée, jamais par l'écart au suivant.

    Déduire la période de l'écart reviendrait à prendre un trou pour un rythme :
    les trois heures manquantes de l'historique deviendraient des périodes de 2 h.
    """
    return 1 if ts_ms >= HOURLY_SINCE_MS else EIGHT_HOURS


def as_funding(rows: list[HlFunding]) -> list[Funding]:
    """Les lignes HL dans la forme commune aux deux places.

    La période vient de la règle ci-dessus. Pendant la phase 8 h, le taux sort
    **tel quel** : personne n'a encore tranché s'il vaut pour 8 h ou pour 1 h.
    """
    return [
        Funding(ts_ms=row.ts_ms, interval_hours=declared_period_hours(row.ts_ms), rate=row.rate)
        for row in rows
    ]


def parse_rows(payload: object) -> list[dict[str, Any]]:
    """Les lignes d'une réponse, ou un refus nommé.

    L'API répond parfois un objet d'erreur avec un code 200 — le même piège que
    le SDK côté bot. Une réponse qui n'est pas une liste de lignes est refusée
    ici, avant d'atteindre le disque.
    """
    # Un objet là où une liste est attendue, c'est un refus de la place, pas une
    # erreur de type du programme : d'où une erreur nommée et non un TypeError.
    if not isinstance(payload, list):
        raise HlError(f"réponse fundingHistory inattendue : {str(payload)[:120]}")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not {"time", "fundingRate", "premium"} <= item.keys():
            raise HlError(f"ligne de funding HL inattendue : {str(item)[:120]}")
        if item.get("coin") != COIN:
            raise HlError(f"ligne d'un autre actif : {item.get('coin')!r}")
        rows.append(item)
    return rows


def to_hl_funding(raw: list[dict[str, Any]]) -> list[HlFunding]:
    try:
        return [
            HlFunding(
                ts_ms=int(item["time"]),
                rate=float(item["fundingRate"]),
                premium=float(item["premium"]),
            )
            for item in raw
        ]
    except (TypeError, ValueError) as e:
        raise HlError(f"ligne de funding HL illisible : {e}") from e


def request_page(client: httpx.Client, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    body = {"type": "fundingHistory", "coin": COIN, "startTime": start_ms, "endTime": end_ms}
    response = client.post(INFO_URL, json=body)
    response.raise_for_status()
    return parse_rows(response.json())


def fetch_month_rows(client: httpx.Client, year: int, month: int) -> list[dict[str, Any]]:
    """Un mois entier, page après page, dans l'ordre et sans doublon."""
    start, end = month_bounds_ms(year, month)
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        page = request_page(client, cursor, end)
        if not page:
            break
        rows.extend(page)
        last = int(page[-1]["time"])
        if last < cursor:  # l'API ne recule pas ; s'y fier serait une boucle sans fin
            raise HlError(f"pagination HL en arrière : {last} < {cursor}")
        cursor = last + 1
        if len(page) < PAGE_LIMIT:
            break
    return rows


def store_month(root: Path, year: int, month: int, raw: list[dict[str, Any]]) -> Path:
    """Écrire un mois par renommage atomique, les lignes telles que servies."""
    path = month_file(root, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "coin": COIN,
        "month": f"{year:04d}-{month:02d}",
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "rows": raw,
    }
    partial = path.with_name(path.name + _PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document, indent=1), encoding="ascii")
    os.replace(partial, path)
    return path


def read_month(root: Path, year: int, month: int) -> list[HlFunding]:
    """Un mois du cache, revalidé : l'ordre et l'appartenance au mois se revérifient."""
    path = month_file(root, year, month)
    if not path.is_file():
        raise CacheMissError(f"funding HL {year:04d}-{month:02d} absent du cache {root}")
    document = json.loads(path.read_text(encoding="ascii"))
    rows = to_hl_funding(parse_rows(document.get("rows")))
    start, end = month_bounds_ms(year, month)
    previous = -1
    for row in rows:
        if not start <= row.ts_ms < end:
            raise HlError(f"funding HL {row.ts_ms} hors du mois {year:04d}-{month:02d}")
        if row.ts_ms <= previous:
            raise HlError(f"funding HL non strictement croissant à {row.ts_ms}")
        previous = row.ts_ms
    return rows


def is_cached(root: Path, year: int, month: int) -> bool:
    return month_file(root, year, month).is_file()


def month_is_over(year: int, month: int, now_ms: int) -> bool:
    return month_bounds_ms(year, month)[1] <= now_ms


def fill_range(
    client: httpx.Client,
    root: Path,
    start: Month,
    end: Month,
    *,
    now_ms: int | None = None,
    progress: Progress | None = None,
) -> HlReport:
    """Remplir le cache HL pour la plage, et rendre ce qui s'y trouve.

    Un mois non terminé est servi mais pas écrit. Un mois vide alors que la
    place cotait déjà est signalé : ce n'est pas la même chose qu'un mois
    d'avant le lancement.
    """
    stamp = now_ms if now_ms is not None else int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    report = HlReport()
    for year, month in months(max(start, FIRST_MONTH), end):
        if is_cached(root, year, month):
            report.cached.append((year, month))
            report.rows.extend(read_month(root, year, month))
            state = "cached"
        else:
            raw = fetch_month_rows(client, year, month)
            rows = to_hl_funding(raw)
            if not rows and (year, month) > FIRST_MONTH:
                report.empty_unexpected.append((year, month))
            if month_is_over(year, month, stamp):
                store_month(root, year, month, raw)
                report.fetched.append((year, month))
                state = "fetched"
            else:
                report.not_final.append((year, month))
                state = "partial"
            report.rows.extend(rows)
        if progress is not None:
            progress(state, year, month)
    return report
