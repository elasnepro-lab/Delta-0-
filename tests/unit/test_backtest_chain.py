"""Chantier 7.2 — l'accès on-chain du backtest : transport, temps, événements.

Aucun test ne touche à une vraie chaîne : un nœud en mémoire répond, compte ses
appels et sait tomber en panne. Ce qui est épinglé n'est pas le confort mais les
trois façons dont cette couche pourrait mentir — rendre une plage de logs à
moitié, prendre un refus de nœud pour une réponse, ou se tromper de bloc parce
qu'elle aurait supposé un rythme constant là où la chaîne en a changé.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.chain import Chain, ChainError, topic_for_address, word
from tests.fake_chain import (
    ALIVE,
    GENESIS_TS,
    POOL,
    SPARE,
    TOPIC,
    FakeNode,
    chain_on,
    reference_block,
)

# --- Le transport -------------------------------------------------------------


def test_a_burst_of_429_is_retried_then_served(tmp_path: Path) -> None:
    node = FakeNode(transient=2)
    pauses: list[float] = []
    chain = Chain("essai", node.client(), endpoints=(ALIVE,), root=tmp_path, pause=pauses.append)

    assert chain.block_number() == node.head
    assert len(pauses) == 2, "un 429 doit faire attendre avant de réessayer"


def test_a_dead_endpoint_falls_over_to_the_next(tmp_path: Path) -> None:
    node = FakeNode(down=("premier",))
    chain = chain_on(node, tmp_path, endpoints=(ALIVE, SPARE))

    assert chain.block_number() == node.head
    assert any(SPARE in url for url in node.urls)


def test_when_nothing_answers_it_refuses_rather_than_guesses(tmp_path: Path) -> None:
    node = FakeNode(down=("premier", "second"))
    chain = chain_on(node, tmp_path, endpoints=(ALIVE, SPARE))

    with pytest.raises(ChainError, match="sans réponse utilisable"):
        chain.block_number()


def test_a_refusal_served_with_a_200_is_not_taken_for_data(tmp_path: Path) -> None:
    """Un nœud qui refuse répond 200 avec un objet d'erreur : ce n'est pas un résultat."""
    node = FakeNode(refuse_all=True)
    chain = chain_on(node, tmp_path)

    with pytest.raises(ChainError, match="pruned history"):
        chain.block_number()


# --- Le temps -----------------------------------------------------------------


def test_the_block_for_an_instant_is_the_last_one_not_after_it(tmp_path: Path) -> None:
    node = FakeNode(head=1_000_000)
    chain = chain_on(node, tmp_path)
    target = GENESIS_TS + 500_000 * 12 + 5  # au milieu d'un bloc

    found = chain.block_at(target)

    assert found == reference_block(node, target)
    assert node.timestamp(found) <= target < node.timestamp(found + 1)


def test_the_search_costs_a_handful_of_probes_not_a_dichotomy(tmp_path: Path) -> None:
    """Sur 500 millions de blocs, une dichotomie demanderait ~29 sondages."""
    node = FakeNode(head=500_000_000)
    chain = chain_on(node, tmp_path)

    chain.block_at(GENESIS_TS + 123_456_789 * 12)

    assert chain.calls <= 12, f"{chain.calls} appels, l'interpolation ne converge pas"


def test_a_chain_whose_pace_changed_a_hundredfold_stays_cheap(tmp_path: Path) -> None:
    """Le cas réel : sur Arbitrum, la seule interpolation a coûté 602 appels pour deux dates.

    Une cadence qui change d'un facteur cent (migration Nitro) rend le rapport
    mesuré sur tout l'intervalle trompeur : sans dichotomie de secours, chaque
    sondage ne resserre presque rien et la campagne devient impossible.
    """
    node = FakeNode(head=500_000_000, pivot=250_000_000, seconds_before_pivot=100)
    chain = chain_on(node, tmp_path)
    target = node.timestamp(400_000_000) + 3

    found = chain.block_at(target)

    assert found == reference_block(node, target)
    assert chain.calls <= 70, f"{chain.calls} appels pour une seule date"


def test_a_chain_that_changed_pace_is_still_read_right(tmp_path: Path) -> None:
    """La fusion d'Ethereum et la migration Nitro ont changé le rythme des blocs."""
    node = FakeNode(head=2_000_000, pivot=1_000_000, seconds_before_pivot=13)
    chain = chain_on(node, tmp_path)

    for target in (GENESIS_TS + 100, node.timestamp(1_500_000) + 7, node.timestamp(999_999)):
        assert chain.block_at(target) == reference_block(node, target)


def test_an_instant_before_the_first_block_is_refused(tmp_path: Path) -> None:
    chain = chain_on(FakeNode(), tmp_path)
    with pytest.raises(ChainError, match="avant le premier bloc"):
        chain.block_at(GENESIS_TS - 1)


def test_an_instant_past_the_head_gives_the_head(tmp_path: Path) -> None:
    node = FakeNode()
    chain = chain_on(node, tmp_path)
    assert chain.block_at(node.timestamp(node.head) + 10_000) == node.head


def test_the_timestamps_survive_the_run(tmp_path: Path) -> None:
    """Une campagne coupée ne redemande pas les blocs qu'elle avait déjà datés."""
    node = FakeNode(head=1_000_000)
    target = GENESIS_TS + 400_000 * 12
    found = chain_on(node, tmp_path).block_at(target)

    repris = chain_on(node, tmp_path)
    assert repris.block_at(target) == found
    assert repris.calls < 4, "les horodatages déjà connus ont été redemandés"


def test_an_unreadable_cache_is_redone_not_trusted(tmp_path: Path) -> None:
    node = FakeNode()
    chain_on(node, tmp_path).block_at(GENESIS_TS + 1_000)
    (tmp_path / "essai-blocks.json").write_text("pas du json", encoding="ascii")

    chain = chain_on(node, tmp_path)
    assert chain.block_at(GENESIS_TS + 1_000) == reference_block(node, GENESIS_TS + 1_000)


# --- Les événements -----------------------------------------------------------


def test_a_dense_range_is_split_until_it_answers(tmp_path: Path) -> None:
    """Une plage trop dense expire : on la coupe, on ne la tronque pas."""
    node = FakeNode(head=1_000_000, max_span=50_000, log_every=10_000)
    chain = chain_on(node, tmp_path)

    logs = chain.logs_range(POOL, [TOPIC], 0, 199_999, chunk=200_000)

    blocks = [int(entry["blockNumber"], 16) for entry in logs]
    assert blocks == sorted(blocks)
    assert blocks == list(range(0, 200_000, 10_000)), "la plage doit être rendue entière"


def test_a_range_that_never_answers_is_refused_not_halved_away(tmp_path: Path) -> None:
    """Rendre la moitié d'une plage ferait un mois sans emprunt, donc un portage gratuit."""
    node = FakeNode(head=1_000_000, max_span=1)
    chain = chain_on(node, tmp_path)

    with pytest.raises(ChainError, match="log query timed out"):
        chain.logs_range(POOL, [TOPIC], 0, 199_999, chunk=200_000)


def test_the_range_is_walked_in_chunks(tmp_path: Path) -> None:
    node = FakeNode(head=1_000_000, log_every=100_000)
    chain = chain_on(node, tmp_path)

    logs = chain.logs_range(POOL, [TOPIC], 0, 999_999, chunk=200_000)

    assert node.methods.count("eth_getLogs") == 5
    assert len(logs) == 10


# --- Le décodage --------------------------------------------------------------


def test_the_words_of_a_log_are_read_in_order() -> None:
    """Les cinq mots de ReserveDataUpdated, dans l'ordre lu sur un vrai log."""
    mots = [27036 * 10**20, 0, 36539 * 10**20, 11784065233 * 10**17, 12422347824 * 10**17]
    data = "0x" + "".join(f"{mot:064x}" for mot in mots)

    assert [word(data, index) for index in range(5)] == mots
    with pytest.raises(ChainError, match="trop court"):
        word(data, 5)


def test_an_address_becomes_an_indexed_topic() -> None:
    topic = topic_for_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
    assert topic == "0x000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e5831"
    assert len(topic) == 66
