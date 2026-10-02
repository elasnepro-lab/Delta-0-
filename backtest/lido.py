"""Le taux de conversion wstETH/stETH, lu sur la chaîne à des blocs passés.

C'est la seule chose dont le backtest a besoin du côté Lido, et c'est mieux
qu'un APR : `stEthPerToken()` est le rapport exact qu'un wstETH vaut en stETH à
un instant donné, net des frais Lido. Le collatéral du montage se multiplie par
ce rapport — inutile d'intégrer un taux annuel qu'il faudrait ensuite supposer
composé d'une façon ou d'une autre.

Vérifié le 2026-09-16 sur l'archive mainnet : bloc 11888477 rend 1,003748, bloc
17000000 rend 1,117817. Les deux collent aux valeurs relevées lors de
l'inventaire, à la sixième décimale.

Deux choses à ne pas confondre :

- **une baisse du rapport n'est pas une anomalie.** Lido peut être pénalisé
  (slashing), et c'est précisément le stress que le short ETH ne couvre pas.
  Une baisse est donc gardée telle quelle ; seule une valeur impossible est
  refusée ;
- **rien avant le 2021-02-20.** Le premier bloc où le contrat répond est le
  11888477, le 2021-02-19 à **16:37 UTC** — cherché par dichotomie le
  2026-09-16, le bloc précédent rendant `0x`. Échantillonner à minuit, le
  premier jour lisible est donc le lendemain. Prendre la date du bloc de
  déploiement aurait demandé un `eth_call` seize heures trop tôt, et c'est
  exactement ce que la garde de `call_at` a attrapé.

Les points sont gardés sur disque au fil de l'eau : deux mille lectures
journalières coûtent cher une fois, jamais deux.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from backtest.chain import Chain

# Une campagne Lido se compte en jours, pas en mois : son avancement se dit
# (état, jour), là où les archives mensuelles disent (état, année, mois).
DayProgress = Callable[[str, str], None]

WSTETH_MAINNET = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
RATIO_SELECTOR = "0x035faf82"  # sélecteur de stEthPerToken(), recalculé le 2026-09-16

FIRST_DAY = "2021-02-20"  # premier minuit où le contrat répond (déployé la veille à 16:37 UTC)
WEI = 10**18
FLUSH_EVERY = 10  # points, pour qu'une coupure ne coûte pas la campagne

_PARTIAL_SUFFIX = ".partial"


class LidoError(Exception):
    """Ce que la chaîne a rendu ne peut pas être un rapport de conversion."""


@dataclass(frozen=True, slots=True)
class Point:
    day: str  # AAAA-MM-JJ, à 00:00 UTC
    block: int
    raw: int  # stEthPerToken en wei, exact

    @property
    def ratio(self) -> float:
        return self.raw / WEI


def cache_file(root: Path) -> Path:
    return root / "lido" / "steth-per-token.json"


def day_start_ts(day: str) -> int:
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp())


def days(first: str, last: str) -> Iterator[str]:
    """Chaque jour de `first` à `last`, bornes comprises."""
    current = dt.date.fromisoformat(first)
    stop = dt.date.fromisoformat(last)
    while current <= stop:
        yield current.isoformat()
        current += dt.timedelta(days=1)


def load(root: Path) -> dict[str, Point]:
    path = cache_file(root)
    if not path.is_file():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="ascii"))
        return {
            day: Point(day=day, block=int(entry["block"]), raw=int(entry["raw"]))
            for day, entry in stored.items()
        }
    except (ValueError, TypeError, KeyError, OSError):
        return {}  # un cache illisible se refait, il ne se devine pas


def save(root: Path, points: dict[str, Point]) -> Path:
    path = cache_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        point.day: {"block": point.block, "raw": point.raw}
        for point in sorted(points.values(), key=lambda p: p.day)
    }
    partial = path.with_name(path.name + _PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document, indent=1), encoding="ascii")
    os.replace(partial, path)
    return path


def read_at(chain: Chain, block: int) -> int:
    raw = chain.call_at(WSTETH_MAINNET, RATIO_SELECTOR, block)
    if raw <= 0:
        raise LidoError(f"stEthPerToken vaut {raw} au bloc {block}")
    return raw


def ensure_days(
    chain: Chain,
    root: Path,
    first: str,
    last: str,
    *,
    progress: DayProgress | None = None,
) -> list[Point]:
    """Le rapport à 00:00 UTC pour chaque jour de la plage, lu une fois pour toutes."""
    if first < FIRST_DAY:
        raise LidoError(f"rien de vérifié avant le {FIRST_DAY} ; {first} demandé")

    points = load(root)
    fresh = 0
    for day in days(first, last):
        if day in points:
            if progress is not None:
                progress("cached", day)
            continue
        block = chain.block_at(day_start_ts(day))
        points[day] = Point(day=day, block=block, raw=read_at(chain, block))
        fresh += 1
        if progress is not None:
            progress("fetched", day)
        if fresh % FLUSH_EVERY == 0:
            save(root, points)
    if fresh:
        save(root, points)
    return [points[day] for day in days(first, last)]


def growth(first: Point, last: Point) -> float:
    """Ce que le collatéral a gagné entre deux points, en proportion.

    C'est la grandeur que le backtest applique ; aucun APR n'a besoin d'être
    supposé, ni sa composition choisie.
    """
    return last.raw / first.raw - 1.0


def drops(points: list[Point]) -> list[tuple[str, float]]:
    """Les jours où le rapport a BAISSÉ, avec l'ampleur.

    Gardés, jamais lissés : une baisse est un slashing, c'est-à-dire le stress
    que la jambe short ne couvre pas.
    """
    return [
        (following.day, following.raw / current.raw - 1.0)
        for current, following in pairwise(points)
        if following.raw < current.raw
    ]
