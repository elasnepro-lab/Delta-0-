"""Chantier 7.2 — le coût de l'emprunt Aave, relevé heure par heure.

Le point défendu ici n'est pas le taux mais l'index : `variableBorrowIndex` est
un cumul, donc l'intérêt couru entre deux relevés est le rapport de leurs index
— rien à intégrer, aucune convention de composition à choisir. C'est ce qui rend
un relevé horaire suffisant là où tous les événements coûteraient 60 Mo par mois.

Le reste protège quatre refus : rejouer une réserve avant son listage, inventer
un index avant le premier relevé, laisser passer un index qui décroît, et
combler une heure sans événement.
"""

from __future__ import annotations

import calendar
import json
from pathlib import Path

import pytest

from backtest.aave_rates import (
    RAY,
    RESERVES,
    TOPIC_RESERVE_DATA_UPDATED,
    WINDOWS,
    AaveRatesError,
    Mark,
    borrow_growth,
    check,
    decode,
    ensure_range,
    hours,
    index_at,
    is_cached,
    month_bounds_s,
    month_file,
    months,
    read_month,
    sample_hour,
)
from tests.fake_chain import FakeNode, chain_on, reserve_data

USDC = RESERVES["usdc-arbitrum"]
WSTETH = RESERVES["wsteth-arbitrum"]

JULY_2023 = (2023, 7)


def day_ts(day: str) -> int:
    year, month, number = (int(part) for part in day.split("-"))
    return calendar.timegm((year, month, number, 0, 0, 0))


AFTER_JULY = day_ts("2023-08-02")  # juillet est clos, le mois peut être figé
DURING_JULY = day_ts("2023-07-10")  # le mois court encore


def node_placing(block: int, at_ts: int, *, log_every: int = 100) -> FakeNode:
    """Un nœud à une seconde par bloc, placé pour que `block` tombe à cet instant.

    Les numéros de bloc des réserves sont réels ; c'est la fausse chaîne qui se
    cale sur eux, sans quoi le mois testé tomberait avant le listage et le test
    mesurerait le refus au lieu de la lecture. `log_every` fait la densité : 100
    blocs entre deux événements imitent l'USDC, 50 000 une réserve calme.
    """
    return FakeNode(
        head=200_000_000, genesis_ts=at_ts - block, seconds_per_block=1, log_every=log_every
    )


def busy_node() -> FakeNode:
    """Une chaîne où l'USDC natif est listé à sa vraie date, le 2023-06-28."""
    return node_placing(USDC.first_block, day_ts("2023-06-28"))


def mark(ts: int, index: int, *, stale: int = 0) -> Mark:
    return Mark(
        ts=ts,
        block=1_000,
        liquidity_rate=RAY // 50,
        variable_borrow_rate=RAY // 25,
        liquidity_index=RAY,
        variable_borrow_index=index,
        stale_blocks=stale,
    )


# --- Lire un événement --------------------------------------------------------


def test_the_five_words_are_read_in_the_published_order() -> None:
    [read] = decode([{"blockNumber": hex(1_000), "data": reserve_data(1_000)}])

    assert read.block == 1_000
    assert read.variable_borrow_rate == 4 * RAY // 100
    assert read.variable_borrow_index == RAY + 1_000 * 2 * 10**18


def test_a_log_that_cannot_be_read_stops_everything() -> None:
    with pytest.raises(AaveRatesError, match="inexploitable"):
        decode([{"blockNumber": None, "data": "0x00"}])


# --- Le relevé horaire --------------------------------------------------------


def test_an_hour_keeps_the_last_index_published_before_it(tmp_path: Path) -> None:
    node = busy_node()
    chain = chain_on(node, tmp_path)
    ts = day_ts("2023-07-05")

    taken = sample_hour(chain, USDC, ts, head=node.head)

    assert taken is not None
    assert taken.ts == ts
    assert taken.block <= chain.block_at(ts, head=node.head)
    assert taken.stale_blocks < node.log_every, "un événement plus récent existait"


def test_a_quiet_reserve_widens_the_window_instead_of_giving_up(tmp_path: Path) -> None:
    """Mesuré : 2 000 blocs suffisent pour l'USDC, le wstETH en demande 20 000."""
    node = node_placing(WSTETH.first_block, day_ts("2023-03-15"), log_every=10_000)
    chain = chain_on(node, tmp_path)

    taken = sample_hour(chain, WSTETH, day_ts("2023-03-20"), head=node.head)

    assert taken is not None
    # Fenêtre étroite vide veut dire : le dernier événement est au moins à sa
    # largeur du repère. L'égalité est possible, d'où le >= .
    assert taken.stale_blocks >= WINDOWS[0], "le relevé aurait dû élargir la fenêtre"
    largeurs = {int(q["toBlock"], 16) - int(q["fromBlock"], 16) + 1 for q in node.queries}
    assert WINDOWS[0] in largeurs, "la fenêtre étroite aurait dû être tentée d'abord"
    assert WINDOWS[1] in largeurs, "puis la suivante, puisque la première ne rendait rien"


def test_a_dormant_reserve_leaves_the_hour_missing_never_filled(tmp_path: Path) -> None:
    """Une heure sans aucun événement est déclarée manquante, pas comblée."""
    node = node_placing(USDC.first_block, day_ts("2023-06-28"), log_every=50_000_000)
    chain = chain_on(node, tmp_path)

    assert sample_hour(chain, USDC, day_ts("2023-07-05"), head=node.head) is None


def test_the_query_carries_the_topic_and_the_reserve(tmp_path: Path) -> None:
    node = busy_node()
    sample_hour(chain_on(node, tmp_path), USDC, day_ts("2023-07-05"), head=node.head)

    topics = node.queries[0]["topics"]
    assert topics[0] == TOPIC_RESERVE_DATA_UPDATED
    assert topics[1].endswith(USDC.asset[2:].lower())


def test_the_window_never_reaches_before_the_reserve_existed(tmp_path: Path) -> None:
    node = node_placing(WSTETH.first_block, day_ts("2023-03-15"), log_every=10_000)
    chain = chain_on(node, tmp_path)

    sample_hour(chain, WSTETH, day_ts("2023-03-15") + 3_600, head=node.head)

    assert min(int(q["fromBlock"], 16) for q in node.queries) >= WSTETH.first_block


# --- Les refus ----------------------------------------------------------------


def test_an_index_that_falls_is_a_corrupted_reading() -> None:
    """Un cumul ne décroît pas : s'il décroît, le portage paraîtrait gratuit."""
    with pytest.raises(AaveRatesError, match="index d'emprunt en baisse"):
        check([mark(10, RAY * 2), mark(20, RAY)], USDC)


def test_marks_that_go_backwards_are_refused() -> None:
    with pytest.raises(AaveRatesError, match="repères non croissants"):
        check([mark(20, RAY), mark(10, RAY * 2)], USDC)


def test_a_reserve_is_never_replayed_before_it_was_listed(tmp_path: Path) -> None:
    """L'USDC natif n'existe sur Aave Arbitrum que depuis le 2023-06-28."""
    chain = chain_on(busy_node(), tmp_path)

    with pytest.raises(AaveRatesError, match="2023-06-28"):
        ensure_range(chain, tmp_path, USDC, (2022, 1), (2022, 3))


def test_an_index_before_the_first_mark_is_not_invented() -> None:
    marks = [mark(1_000, RAY), mark(2_000, RAY * 2)]

    assert index_at(marks, 1_500) == RAY
    assert index_at(marks, 2_000) == RAY * 2
    with pytest.raises(AaveRatesError, match="mois précédent"):
        index_at(marks, 999)


# --- Ce que le backtest en fait -----------------------------------------------


def test_the_cost_of_carrying_is_the_ratio_of_two_indices() -> None:
    """Aucun APR n'est intégré : l'intérêt couru est publié, il se lit."""
    marks = [mark(1_000, RAY), mark(2_000, RAY * 105 // 100)]

    assert borrow_growth(marks, 1_000, 2_000) == pytest.approx(0.05)
    assert borrow_growth(marks, 1_000, 1_999) == pytest.approx(0.0)


def test_a_range_the_wrong_way_round_is_refused() -> None:
    with pytest.raises(AaveRatesError, match="à l'envers"):
        borrow_growth([mark(1_000, RAY)], 2_000, 1_000)


# --- Le cache -----------------------------------------------------------------


def test_a_finished_month_is_cached_and_costs_nothing_twice(tmp_path: Path) -> None:
    node = busy_node()
    report = ensure_range(
        chain_on(node, tmp_path), tmp_path, USDC, JULY_2023, JULY_2023, now_ts=AFTER_JULY
    )

    assert report.fetched == [JULY_2023]
    assert len(report.marks) == len(hours(2023, 7)) == 744
    assert report.missing_hours == []
    assert is_cached(tmp_path, USDC, 2023, 7)

    repris = chain_on(node, tmp_path)
    again = ensure_range(repris, tmp_path, USDC, JULY_2023, JULY_2023, now_ts=AFTER_JULY)

    assert again.cached == [JULY_2023]
    assert [m.ts for m in again.marks] == [m.ts for m in report.marks]
    assert repris.calls == 0, "un mois déjà relevé est repassé par la chaîne"


def test_the_running_month_is_served_but_never_frozen(tmp_path: Path) -> None:
    node = busy_node()
    report = ensure_range(
        chain_on(node, tmp_path), tmp_path, USDC, JULY_2023, JULY_2023, now_ts=DURING_JULY
    )

    assert report.not_final == [JULY_2023]
    assert not is_cached(tmp_path, USDC, 2023, 7)


def test_a_cached_month_holding_a_falling_index_is_refused(tmp_path: Path) -> None:
    """Le cache n'est pas cru sur parole : il repasse par le même contrôle."""
    path = month_file(tmp_path, USDC, 2023, 7)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "reserve": USDC.name,
                "month": "2023-07",
                "marks": [[10, 1, 0, 0, RAY, RAY * 2, 0], [20, 2, 0, 0, RAY, RAY, 0]],
            }
        ),
        encoding="ascii",
    )

    with pytest.raises(AaveRatesError, match="index d'emprunt en baisse"):
        read_month(tmp_path, USDC, 2023, 7)


def test_the_staleness_of_every_mark_is_kept(tmp_path: Path) -> None:
    """Un relevé dit de combien il précède son repère : c'est sa part d'imprécision."""
    node = busy_node()
    report = ensure_range(
        chain_on(node, tmp_path), tmp_path, USDC, JULY_2023, JULY_2023, now_ts=AFTER_JULY
    )

    assert report.worst_stale_blocks < node.log_every
    relus, _ = read_month(tmp_path, USDC, 2023, 7)
    assert [m.stale_blocks for m in relus] == [m.stale_blocks for m in report.marks]


# --- Le calendrier ------------------------------------------------------------


def test_the_months_of_a_range_are_walked_across_the_year() -> None:
    assert months((2022, 11), (2023, 2)) == [(2022, 11), (2022, 12), (2023, 1), (2023, 2)]


def test_a_month_runs_from_its_first_second_to_the_next_one() -> None:
    start, end = month_bounds_s(2023, 7)
    assert end - start == 31 * 24 * 3600
    assert month_bounds_s(2023, 8)[0] == end


def test_every_hour_of_the_month_gets_a_mark() -> None:
    marks = hours(2023, 7)
    assert len(marks) == 744
    assert marks[0] == month_bounds_s(2023, 7)[0]
    assert marks[-1] == month_bounds_s(2023, 7)[1] - 3_600
