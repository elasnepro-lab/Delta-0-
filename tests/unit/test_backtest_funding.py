"""Chantier 7.2 — le funding Binance, lu dans les mêmes archives que les prix.

Ce qui est épinglé ici tient en deux idées. La période que couvre un taux est
LUE sur la ligne, jamais supposée : un taux 8 h et un taux horaire ne se
somment pas pareil, et les deux places n'ont pas toujours servi la même
période. Et un trou se mesure, il ne se comble pas : un versement manquant
n'est pas un versement nul tant que personne ne l'a tranché.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backtest.binance import (
    ArchiveError,
    Funding,
    month_bounds_ms,
    parse_funding_csv,
    read_funding_archive,
)
from backtest.cache import CacheMissError, store
from backtest.funding import FUNDING, HOUR_MS, gaps, intervals, load, read_funding_month
from tests.binance_archives import build_funding_csv, build_zip, funding_archive

OCTOBER_START = month_bounds_ms(2025, 10)[0]


def cache_month(root: Path, year: int, month: int, payload: bytes | None = None) -> bytes:
    """Poser un mois de funding dans le cache, comme un téléchargement l'aurait fait."""
    payload = funding_archive(year, month) if payload is None else payload
    store(root, FUNDING, year, month, payload, hashlib.sha256(payload).hexdigest())
    return payload


# --- Lire une ligne -----------------------------------------------------------


def test_the_interval_is_read_from_the_row_not_assumed() -> None:
    rows = parse_funding_csv(build_funding_csv(2, start=OCTOBER_START, interval_hours=8))
    assert [row.interval_hours for row in rows] == [8, 8]
    hourly = parse_funding_csv(build_funding_csv(2, start=OCTOBER_START, interval_hours=1))
    assert [row.interval_hours for row in hourly] == [1, 1]


def test_the_header_and_the_rate_are_read_as_published() -> None:
    rows = parse_funding_csv(build_funding_csv(1, start=OCTOBER_START, drift_ms=7))
    assert rows == [Funding(ts_ms=OCTOBER_START + 7, interval_hours=8, rate=-0.00001572)]


def test_an_unreadable_funding_line_stops_everything() -> None:
    with pytest.raises(ArchiveError, match="funding illisible"):
        parse_funding_csv("1759276800000,8,pas-un-taux\n")


# --- Lire un mois -------------------------------------------------------------


def test_a_month_of_funding_reads_whole(tmp_path: Path) -> None:
    payload = cache_month(tmp_path, 2025, 10)
    digest = hashlib.sha256(payload).hexdigest()

    assert len(read_funding_archive(payload, digest, 2025, 10)) == 3
    assert len(read_funding_month(tmp_path, 2025, 10)) == 3


def test_the_millisecond_drift_is_not_taken_for_a_fault() -> None:
    """Les horodatages réels dérivent (1761926400007) : aucune grille ne s'applique."""
    payload = funding_archive(2025, 10)
    rows = read_funding_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)
    assert rows[0].ts_ms % HOUR_MS != 0


@pytest.mark.parametrize(
    ("csv", "match"),
    [
        (build_funding_csv(2, start=OCTOBER_START - 9 * HOUR_MS), "hors du mois"),
        (
            build_funding_csv(1, start=OCTOBER_START) + f"{OCTOBER_START},8,-0.00001\n",
            "croissant",
        ),
        (
            f"calc_time,funding_interval_hours,last_funding_rate\n{OCTOBER_START},0,-0.00001\n",
            "intervalle de funding impossible",
        ),
    ],
)
def test_funding_that_does_not_belong_to_the_month_is_refused(csv: str, match: str) -> None:
    payload = build_zip(csv, name="ETHUSDT-fundingRate-2025-10.csv")
    with pytest.raises(ArchiveError, match=match):
        read_funding_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)


# --- Recoudre les mois --------------------------------------------------------


def test_a_range_is_stitched_in_order(tmp_path: Path) -> None:
    for month in (9, 10, 11):
        cache_month(tmp_path, 2025, month)

    rows = load(tmp_path, (2025, 9), (2025, 11))

    assert len(rows) == 9
    assert rows == sorted(rows, key=lambda row: row.ts_ms)


def test_a_missing_month_is_never_stitched_over(tmp_path: Path) -> None:
    """Une série à trou se sommerait sans rien dire : le portage paraîtrait moins cher."""
    cache_month(tmp_path, 2025, 9)
    cache_month(tmp_path, 2025, 11)

    with pytest.raises(CacheMissError, match="2025-10"):
        load(tmp_path, (2025, 9), (2025, 11))


# --- Décrire la série, pas la corriger ----------------------------------------


def test_the_periods_are_counted_as_they_are(tmp_path: Path) -> None:
    cache_month(tmp_path, 2025, 9, funding_archive(2025, 9, rows=2, interval_hours=8))
    cache_month(tmp_path, 2025, 10, funding_archive(2025, 10, rows=4, interval_hours=1))

    rows = load(tmp_path, (2025, 9), (2025, 10))

    assert intervals(rows) == {8: 2, 1: 4}


def test_a_hole_is_measured_against_the_period_the_row_declares() -> None:
    """Un changement de rythme n'est pas une panne : c'est la ligne qui dit ce qu'elle couvre."""
    payload = funding_archive(2025, 10, rows=3, interval_hours=1)
    rows = read_funding_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)
    assert gaps(rows) == []

    pierced = [rows[0], rows[2]]  # l'heure du milieu manque
    assert gaps(pierced) == [(rows[0].ts_ms, 1.0)]


def test_a_drift_of_milliseconds_is_not_a_hole() -> None:
    first = Funding(ts_ms=OCTOBER_START, interval_hours=8, rate=0.0)
    late = Funding(ts_ms=OCTOBER_START + 8 * HOUR_MS + 400, interval_hours=8, rate=0.0)
    assert gaps([first, late]) == []
