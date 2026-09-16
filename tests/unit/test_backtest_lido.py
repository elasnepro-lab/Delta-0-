"""Chantier 7.2 — le rapport wstETH/stETH lu sur la chaîne, jour après jour.

Le nœud est en mémoire, et son `eth_call` rend un rapport qui croît avec le
bloc : on peut donc vérifier que le bon bloc a été choisi pour chaque jour. Deux
points comptent plus que les autres — une baisse du rapport (slashing) doit
traverser intacte, parce que c'est le stress que le short ne couvre pas ; et une
campagne coupée ne doit pas se repayer.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from backtest.lido import (
    FIRST_DAY,
    WEI,
    LidoError,
    Point,
    cache_file,
    day_start_ts,
    days,
    drops,
    ensure_days,
    growth,
    load,
    save,
)
from tests.fake_chain import FakeNode, chain_on

DAY_S = 86_400


def growing(block: int) -> int:
    """Un rapport qui monte d'un peu à chaque bloc : 1,0 au bloc 0."""
    return WEI + block * 10**9


def node_with_days() -> FakeNode:
    """Un nœud dont le bloc 0 tombe au premier jour vérifié, à 00:00 UTC, 12 s par bloc."""
    return FakeNode(
        head=2_000_000,
        genesis_ts=day_start_ts(FIRST_DAY),
        seconds_per_block=12,
        call_value=growing,
    )


def test_the_days_of_a_range_are_walked_once() -> None:
    assert list(days("2021-03-01", "2021-03-04")) == [
        "2021-03-01",
        "2021-03-02",
        "2021-03-03",
        "2021-03-04",
    ]
    assert list(days("2021-03-01", "2021-03-01")) == ["2021-03-01"]


def test_a_day_is_taken_at_midnight_utc() -> None:
    assert day_start_ts("2021-02-19") % DAY_S == 0
    assert day_start_ts("2021-02-20") - day_start_ts("2021-02-19") == DAY_S


def test_nothing_is_invented_before_the_first_verified_reading(tmp_path: Path) -> None:
    """La date de déploiement du contrat est antérieure, mais NON VÉRIFIÉE."""
    chain = chain_on(node_with_days(), tmp_path)
    with pytest.raises(LidoError, match=FIRST_DAY):
        ensure_days(chain, tmp_path, "2021-01-01", "2021-01-05")


def test_each_day_lands_on_the_last_block_of_that_day(tmp_path: Path) -> None:
    node = node_with_days()
    chain = chain_on(node, tmp_path)
    first = FIRST_DAY

    points = ensure_days(chain, tmp_path, first, _plus(first, 3))

    assert [point.day for point in points] == [_plus(first, n) for n in range(4)]
    for point in points:
        assert node.timestamp(point.block) <= day_start_ts(point.day)
        assert node.timestamp(point.block + 1) > day_start_ts(point.day)
        assert point.raw == growing(point.block)


def test_a_reading_of_zero_is_refused(tmp_path: Path) -> None:
    node = FakeNode(head=2_000_000, genesis_ts=day_start_ts(FIRST_DAY), call_value=lambda _: 0)
    chain = chain_on(node, tmp_path)
    first = FIRST_DAY

    with pytest.raises(LidoError, match="stEthPerToken vaut 0"):
        ensure_days(chain, tmp_path, first, first)


# --- Ce qui ne doit pas se repayer --------------------------------------------


def test_a_second_pass_reads_nothing_again(tmp_path: Path) -> None:
    node = node_with_days()
    first = FIRST_DAY
    taken = ensure_days(chain_on(node, tmp_path), tmp_path, first, _plus(first, 5))

    repris = chain_on(node, tmp_path)
    again = ensure_days(repris, tmp_path, first, _plus(first, 5))

    assert again == taken
    assert repris.calls == 0, "un jour déjà lu est repassé par la chaîne"


def test_the_points_are_kept_along_the_way(tmp_path: Path) -> None:
    """Une coupure au milieu d'une campagne de deux mille lectures ne la recommence pas."""
    node = node_with_days()
    first = FIRST_DAY

    ensure_days(chain_on(node, tmp_path), tmp_path, first, _plus(first, 14))

    stored = json.loads(cache_file(tmp_path).read_text(encoding="ascii"))
    assert len(stored) == 15
    assert list(stored) == sorted(stored), "les jours sont rangés dans l'ordre"


def test_an_unreadable_cache_is_redone_not_trusted(tmp_path: Path) -> None:
    cache_file(tmp_path).parent.mkdir(parents=True)
    cache_file(tmp_path).write_text("pas du json", encoding="ascii")
    assert load(tmp_path) == {}


def test_a_stored_point_reads_back_exactly(tmp_path: Path) -> None:
    """Le rapport est gardé en wei : la valeur exacte, pas un flottant arrondi."""
    point = Point(day="2023-04-07", block=17_000_000, raw=1_117_817_123_456_789_012)
    save(tmp_path, {point.day: point})

    assert load(tmp_path)[point.day] == point
    assert load(tmp_path)[point.day].ratio == pytest.approx(1.117817, abs=1e-6)


# --- Ce que le backtest en fait -----------------------------------------------


def test_the_growth_is_the_ratio_itself_no_apr_assumed() -> None:
    """Le collatéral se multiplie par le rapport : aucun taux annuel a supposer."""
    start = Point(day="2023-01-01", block=1, raw=WEI)
    end = Point(day="2024-01-01", block=2, raw=WEI * 104 // 100)

    assert growth(start, end) == pytest.approx(0.04)


def test_a_fall_of_the_ratio_is_reported_never_smoothed() -> None:
    """Une baisse est un slashing Lido : le stress que la jambe short ne couvre pas."""
    serie = [
        Point(day="2023-01-01", block=1, raw=WEI),
        Point(day="2023-01-02", block=2, raw=WEI * 101 // 100),
        Point(day="2023-01-03", block=3, raw=WEI * 99 // 100),
        Point(day="2023-01-04", block=4, raw=WEI * 995 // 1000),
    ]

    fallen = drops(serie)

    assert [day for day, _ in fallen] == ["2023-01-03"]
    assert fallen[0][1] == pytest.approx(-0.0198, abs=1e-4)


def test_a_series_that_only_grows_has_no_fall() -> None:
    serie = [Point(day=f"2023-01-0{n}", block=n, raw=WEI + n) for n in (1, 2, 3)]
    assert drops(serie) == []


# --- Petits secours -----------------------------------------------------------


def _plus(day: str, count: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=count)).isoformat()
