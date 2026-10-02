"""Chantier 7.2 — le prix de marché du stETH, celui de la sortie.

Deux sources qui ne répondent pas à la même question, et les tests le disent :
Chainlink donne ce que l'oracle AFFICHAIT, avec un point par jour, donc un creux
intrajournalier y est invisible ; Curve donne ce qu'un vendeur aurait OBTENU,
glissement compris. Confondre les deux ferait passer le décrochage de juin 2022
pour moins profond qu'il ne fut à la vente.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.chain import ChainError
from backtest.steth import (
    CURVE_POOL,
    DECIMALS_SELECTOR,
    GET_DY_SELECTOR,
    GET_ROUND_SELECTOR,
    WEI,
    Round,
    StethError,
    all_rounds,
    compose,
    ensure_phase,
    ensure_samples,
    load_rounds,
    lowest,
    ratio_at,
    read_round,
    save_rounds,
)
from tests.fake_chain import FakeNode, chain_on

DAY = 86_400
JUNE_2022 = 1_654_000_000  # 2022-05-31, un point de départ quelconque


def words(*values: int) -> str:
    return "0x" + "".join(f"{value:064x}" for value in values)


class FakeFeed:
    """Le proxy Chainlink et le pool Curve, en mémoire.

    Les rounds sont contigus et espacés d'un jour, comme le vrai flux : c'est ce
    rythme qui rend un creux intrajournalier invisible.
    """

    def __init__(
        self,
        *,
        ratios: dict[int, list[int]] | None = None,
        decimals: int = 18,
        curve: dict[int, int] | None = None,
    ) -> None:
        self.ratios = ratios if ratios is not None else {1: [int(0.99 * WEI)] * 3}
        self.decimals = decimals
        self.curve = curve if curve is not None else {}
        self.asked: list[tuple[int, int]] = []

    def answer(self, to: str, data: str, block: int) -> str:
        if to == CURVE_POOL and data.startswith(GET_DY_SELECTOR):
            return words(self.curve.get(block, int(0.94 * WEI)))
        if data.startswith(DECIMALS_SELECTOR):
            return words(self.decimals)
        if data.startswith(GET_ROUND_SELECTOR):
            composed = int(data[10:], 16)
            phase, number = composed >> 64, composed & ((1 << 64) - 1)
            self.asked.append((phase, number))
            published = self.ratios.get(phase, [])
            if not 1 <= number <= len(published):
                return words(0, 0, 0, 0, 0)  # pas encore publié
            ts = JUNE_2022 + (number - 1) * DAY
            return words(composed, published[number - 1], ts, ts, composed)
        raise AssertionError(f"appel inattendu : {data[:10]}")

    def node(self) -> FakeNode:
        return FakeNode(head=1_000_000, call_data=self.answer)


def descending() -> FakeFeed:
    """Un flux qui plonge puis remonte, comme juin 2022."""
    ratios = [int(r * WEI) for r in (0.999, 0.985, 0.960, 0.93502, 0.941, 0.968)]
    return FakeFeed(ratios={1: ratios})


# --- Le flux Chainlink --------------------------------------------------------


def test_the_rounds_are_walked_until_the_first_unpublished_one(tmp_path: Path) -> None:
    feed = descending()
    node = feed.node()

    rounds = ensure_phase(chain_on(node, tmp_path), tmp_path, 1)

    assert [r.number for r in rounds] == [1, 2, 3, 4, 5, 6]
    assert (1, 7) in feed.asked, "il faut demander le round suivant pour savoir qu'il n'existe pas"
    assert rounds[3].ratio == pytest.approx(0.93502)


def test_a_round_is_addressed_by_its_phase_and_its_number() -> None:
    assert compose(1, 304) == (1 << 64) | 304
    assert compose(2, 897) >> 64 == 2


def test_a_second_pass_asks_only_for_what_might_have_appeared(tmp_path: Path) -> None:
    feed = descending()
    node = feed.node()
    first = ensure_phase(chain_on(node, tmp_path), tmp_path, 1)
    feed.asked.clear()

    again = ensure_phase(chain_on(node, tmp_path), tmp_path, 1)

    assert [r.number for r in again] == [r.number for r in first]
    assert feed.asked == [(1, 7)], "seul le round pas encore publié doit être redemandé"


def test_an_unpublished_round_is_not_taken_for_a_price(tmp_path: Path) -> None:
    feed = descending()
    chain = chain_on(feed.node(), tmp_path)

    assert read_round(chain, 1, 7, block=1_000) is None


def test_a_feed_that_changed_its_decimals_is_refused(tmp_path: Path) -> None:
    """Une décimale qui change réécrirait toute la série sans prévenir."""
    feed = FakeFeed(decimals=8)

    with pytest.raises(StethError, match="décimales"):
        ensure_phase(chain_on(feed.node(), tmp_path), tmp_path, 1)


def test_the_two_phases_are_joined_in_time_order(tmp_path: Path) -> None:
    save_rounds(
        tmp_path, 1, [Round(1, 1, JUNE_2022 + DAY, WEI), Round(1, 2, JUNE_2022 + 2 * DAY, WEI)]
    )
    save_rounds(tmp_path, 2, [Round(2, 1, JUNE_2022, WEI)])

    joined = all_rounds(tmp_path)

    assert [(r.phase, r.number) for r in joined] == [(2, 1), (1, 1), (1, 2)]


def test_an_unreadable_cache_is_redone_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "steth" / "chainlink-phase1.json"
    path.parent.mkdir(parents=True)
    path.write_text("pas du json", encoding="ascii")

    assert load_rounds(tmp_path, 1) == []


# --- Ce que l'oracle affichait, et ce qu'il ne montre pas ----------------------


def test_the_value_holds_between_two_rounds(tmp_path: Path) -> None:
    """Un point par jour : entre deux rounds, l'oracle affiche la même chose."""
    rounds = ensure_phase(chain_on(descending().node(), tmp_path), tmp_path, 1)
    creux = rounds[3]

    assert ratio_at(rounds, creux.ts) == pytest.approx(0.93502)
    assert ratio_at(rounds, creux.ts + DAY // 2) == pytest.approx(0.93502)
    assert ratio_at(rounds, creux.ts + DAY) == pytest.approx(0.941)


def test_a_moment_before_the_feed_started_is_refused(tmp_path: Path) -> None:
    rounds = ensure_phase(chain_on(descending().node(), tmp_path), tmp_path, 1)

    with pytest.raises(StethError, match="ne commence pas si tôt"):
        ratio_at(rounds, rounds[0].ts - 1)


def test_the_lowest_round_of_a_window_is_the_published_one(tmp_path: Path) -> None:
    rounds = ensure_phase(chain_on(descending().node(), tmp_path), tmp_path, 1)

    creux = lowest(rounds, rounds[0].ts, rounds[-1].ts)

    assert creux.ratio == pytest.approx(0.93502)
    with pytest.raises(StethError, match="aucun round"):
        lowest(rounds, rounds[-1].ts + DAY, rounds[-1].ts + 2 * DAY)


# --- Ce qu'un vendeur aurait obtenu -------------------------------------------


def test_curve_shows_what_the_daily_feed_cannot(tmp_path: Path) -> None:
    """Le creux d'exécution du 15 juin 2022 est sous le plus bas publié par l'oracle."""
    feed = descending()
    node = feed.node()
    creux_bloc = node.head // 2
    feed.curve = {creux_bloc: int(0.93107 * WEI)}
    chain = chain_on(node, tmp_path)
    ts = chain.timestamp_of(creux_bloc)

    [sample] = ensure_samples(chain, tmp_path, ts, ts, DAY)

    assert sample.block == creux_bloc
    assert sample.ratio == pytest.approx(0.93107)
    assert sample.ratio < 0.93502, "sinon Curve n'apprend rien de plus que l'oracle"


def test_the_samples_are_kept_and_not_read_twice(tmp_path: Path) -> None:
    feed = descending()
    node = feed.node()
    chain = chain_on(node, tmp_path)
    ts = chain.timestamp_of(node.head // 2)

    ensure_samples(chain, tmp_path, ts, ts + DAY, DAY)
    repris = chain_on(node, tmp_path)
    again = ensure_samples(repris, tmp_path, ts, ts + DAY, DAY)

    assert len(again) == 2
    assert repris.calls == 0, "un échantillon déjà pris est repassé par la chaîne"
    stored = json.loads((tmp_path / "steth" / "curve.json").read_text(encoding="ascii"))
    assert len(stored) == 2


def test_an_impossible_step_is_refused(tmp_path: Path) -> None:
    chain = chain_on(descending().node(), tmp_path)
    with pytest.raises(StethError, match="pas d'échantillonnage"):
        ensure_samples(chain, tmp_path, JUNE_2022, JUNE_2022 + DAY, 0)


def test_an_empty_answer_from_the_pool_is_not_a_zero_price(tmp_path: Path) -> None:
    """Une adresse sans code à ce bloc rend `0x` : la garde de la couche chaîne le refuse."""

    def muet(to: str, data: str, block: int) -> str:
        return "0x" if to == CURVE_POOL else words(18)

    node = FakeNode(head=1_000_000, call_data=muet)
    chain = chain_on(node, tmp_path)

    with pytest.raises(ChainError, match="pas de code"):
        ensure_samples(
            chain, tmp_path, chain.timestamp_of(500_000), chain.timestamp_of(500_000), DAY
        )
