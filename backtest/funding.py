"""Le funding : ce que la jambe short paie ou encaisse, période après période.

Le funding Binance voyage dans les mêmes archives mensuelles que les prix, donc
dans le même cache et avec le même contrôle d'empreinte. Ce module ne fait que
le relire et le recoudre d'un mois à l'autre.

Deux choses ne sont jamais supposées ici :

- **la période que couvre un taux** est lue sur la ligne (`funding_interval_hours`),
  parce qu'un taux « 8 h » et un taux horaire ne se somment pas de la même façon
  et qu'aucune des deux places n'a toujours servi la même période ;
- **les trous** sont mesurés et rendus, jamais comblés. Un versement manquant
  n'est pas un versement nul tant que personne ne l'a décidé : c'est une
  question de méthode, pas de code, et elle se tranche au gel de la spec.

Le funding Hyperliquid, lui, n'a aucune archive : il se pagine depuis l'API et
arrive dans un second temps.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

from backtest.binance import SERIES, Funding, Series, months, read_funding_archive
from backtest.cache import Month, cached_bytes

FUNDING: Series = SERIES["funding"]

# Les horodatages dérivent de quelques millisecondes ; une minute de tolérance
# est large et ne peut pas masquer un trou, qui se compte en heures.
DRIFT_TOLERANCE_MS = 60_000
HOUR_MS = 3_600_000


def read_funding_month(root: Path, year: int, month: int) -> list[Funding]:
    """Un mois de funding, revérifié contre l'empreinte gardée à côté de lui."""
    payload, expected = cached_bytes(root, FUNDING, year, month)
    return read_funding_archive(payload, expected, year, month)


def load(root: Path, start: Month, end: Month) -> list[Funding]:
    """Tous les versements de la plage, recousus dans l'ordre.

    Un mois absent du cache lève : une série de funding à trou se sommerait
    sans rien dire, et le coût du portage paraîtrait plus faible qu'il n'est.
    """
    rows: list[Funding] = []
    for year, month in months(start, end):
        taken = read_funding_month(root, year, month)
        if rows and taken and taken[0].ts_ms <= rows[-1].ts_ms:
            raise ValueError(f"funding {year:04d}-{month:02d} chevauche le mois précédent")
        rows.extend(taken)
    return rows


def intervals(rows: Sequence[Funding]) -> dict[int, int]:
    """Combien de lignes pour chaque période déclarée. Décrit la série, ne la corrige pas."""
    counts: dict[int, int] = {}
    for row in rows:
        counts[row.interval_hours] = counts.get(row.interval_hours, 0) + 1
    return counts


def gaps(rows: Sequence[Funding]) -> list[tuple[int, float]]:
    """Les trous : (instant du versement, heures manquantes après lui).

    Mesuré contre la période que la ligne DÉCLARE couvrir, pas contre une
    période supposée constante — c'est ce qui permet de traverser un changement
    de rythme sans le confondre avec une panne.
    """
    holes: list[tuple[int, float]] = []
    for row, following in pairwise(rows):
        elapsed = following.ts_ms - row.ts_ms
        expected = row.interval_hours * HOUR_MS
        if elapsed > expected + DRIFT_TOLERANCE_MS:
            holes.append((row.ts_ms, (elapsed - expected) / HOUR_MS))
    return holes
