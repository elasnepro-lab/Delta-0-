"""Chantier 7.3 — le barème des coûts, et ce qu'il refuse de taire.

Trois choses sont défendues ici, et aucune n'est arithmétique :

- **un paramètre supposé se déclare.** `unverified()` est la liste que le gel de
  la méthode doit aller lire ; un barème où elle se vide toute seule ne sert
  plus à rien.
- **les natures de coût ne se mélangent pas.** Un rapport doit pouvoir dire si
  l'année coûte cher par les frais (le rythme de re-centrage, réglable) ou par
  le glissement (la taille, qui ne l'est pas).
- **la table des jambes vient du README §8.** P2 est local, P6 traverse le pont :
  c'est toute la différence entre la défense rapide du flanc haut et une
  opération de cinq minutes, et elle doit se lire dans le coût.
"""

from __future__ import annotations

from typing import get_args

import pytest

from backtest.costs import (
    BPS,
    DEFAULT,
    GWEI,
    LEGS,
    Charge,
    Param,
    bridge_charge,
    charge,
    gas_usd,
    order_charge,
    swap_charge,
)
from delta0.types import ActionKind

ETH = 3_000.0
AMOUNT = 40_000.0


def test_les_parametres_supposes_sont_nommes() -> None:
    """Le barème par défaut ne prétend pas être mesuré : il dit ce qui ne l'est pas."""
    manquants = DEFAULT.unverified()
    assert "hl_taker_fee" in manquants
    assert "swap_slippage" in manquants
    assert "bridge_fee" in manquants
    # Le seul chiffre vraiment mesuré à ce jour : le gaz du cycle Aave, sur fork.
    assert DEFAULT.aave_gas.verified
    assert "aave_gas" not in manquants


def test_un_parametre_non_verifie_le_dit_a_l_affichage() -> None:
    assert "NON VÉRIFIÉ" in str(DEFAULT.hl_taker_fee)
    assert "NON VÉRIFIÉ" not in str(DEFAULT.aave_gas)


def test_remplacer_un_parametre_par_une_lecture() -> None:
    lu = Param(2.5, "bps", "API Hyperliquid userFees, lu le 2026-09-17", verified=True)
    bareme = DEFAULT.with_param("hl_taker_fee", lu)
    assert bareme.hl_taker_fee.verified
    assert "hl_taker_fee" not in bareme.unverified()
    assert DEFAULT.hl_taker_fee.value != lu.value  # l'original n'a pas bougé


def test_un_parametre_inconnu_refuse() -> None:
    with pytest.raises(KeyError, match="inconnu"):
        DEFAULT.with_param("frais_imaginaires", Param(1.0, "bps", "nulle part"))


# --- les natures ---------------------------------------------------------------


def test_les_natures_restent_separees_a_l_addition() -> None:
    total = Charge(fee_usd=1.0, gas_usd=2.0) + Charge(slippage_usd=4.0, bridge_usd=8.0)
    assert (total.fee_usd, total.slippage_usd, total.bridge_usd, total.gas_usd) == (
        1.0,
        4.0,
        8.0,
        2.0,
    )
    assert total.total_usd == 15.0


def test_l_echange_paie_des_frais_et_un_glissement_distincts() -> None:
    cost = swap_charge(AMOUNT, DEFAULT, ETH)
    assert cost.fee_usd == pytest.approx(AMOUNT * DEFAULT.swap_fee.value * BPS)
    assert cost.slippage_usd == pytest.approx(AMOUNT * DEFAULT.swap_slippage.value * BPS)
    assert cost.gas_usd > 0.0
    assert cost.bridge_usd == 0.0


def test_un_montant_nul_ne_coute_rien_pas_meme_le_gaz() -> None:
    """Une action bornée à zéro par un solde n'a rien envoyé : elle ne paie rien."""
    assert swap_charge(0.0, DEFAULT, ETH).total_usd == 0.0
    assert bridge_charge(0.0, DEFAULT, ETH).total_usd == 0.0
    assert order_charge(0.0, DEFAULT, maker=True).total_usd == 0.0


def test_traverser_coute_plus_cher_que_poster() -> None:
    """Les urgences traversent : leur supposer le maker sous-estimerait ce qui coûte le plus."""
    poste = order_charge(AMOUNT, DEFAULT, maker=True)
    traverse = order_charge(AMOUNT, DEFAULT, maker=False)
    assert traverse.fee_usd > poste.fee_usd


def test_le_pont_a_une_part_fixe_que_les_petits_montants_sentent() -> None:
    petit = bridge_charge(100.0, DEFAULT, ETH)
    assert petit.bridge_usd > DEFAULT.bridge_fixed.value
    assert petit.bridge_usd == pytest.approx(
        100.0 * DEFAULT.bridge_fee.value * BPS + DEFAULT.bridge_fixed.value
    )


def test_le_gaz_se_paie_en_eth_donc_suit_le_prix() -> None:
    cher = gas_usd(DEFAULT.aave_gas.value, DEFAULT, 4_000.0)
    bon_marche = gas_usd(DEFAULT.aave_gas.value, DEFAULT, 2_000.0)
    assert cher == pytest.approx(2 * bon_marche)
    assert bon_marche == pytest.approx(
        DEFAULT.aave_gas.value * DEFAULT.gas_price.value * GWEI * 2_000.0
    )


# --- la table des jambes -------------------------------------------------------


def test_l_ajout_de_marge_est_local_et_ne_coute_rien() -> None:
    """P2 : un appel à Hyperliquid, pas de chaîne, pas de pont, pas d'ordre.

    C'est ce qui en fait la seule défense rapide du flanc haut ; un backtest qui
    lui facturerait un pont la rendrait artificiellement chère et pousserait
    l'arbitrage vers la fermeture partielle, qui ne protège de rien.
    """
    assert charge("ADD_ISOLATED_MARGIN", DEFAULT, eth_price=ETH).total_usd == 0.0


def test_la_pompe_descendante_traverse_le_pont() -> None:
    cost = charge("PUMP_DOWN", DEFAULT, eth_price=ETH, bridged_usd=AMOUNT)
    assert cost.bridge_usd > 0.0
    assert cost.gas_usd > 0.0
    assert cost.fee_usd == 0.0  # aucun ordre : on rembourse, on ne trade pas


def test_le_recentrage_paie_les_quatre_natures() -> None:
    cost = charge(
        "RECENTER_UP",
        DEFAULT,
        eth_price=ETH,
        swapped_usd=AMOUNT,
        bridged_usd=AMOUNT,
        order_usd=AMOUNT,
        maker=True,
    )
    assert cost.fee_usd > 0.0
    assert cost.slippage_usd > 0.0
    assert cost.bridge_usd > 0.0
    assert cost.gas_usd > 0.0


def test_le_desendettement_echange_mais_ne_ponte_pas() -> None:
    """P4 vend du collatéral sur place pour rembourser : rien ne quitte Arbitrum."""
    cost = charge("STEPWISE_DELEVERAGE", DEFAULT, eth_price=ETH, swapped_usd=AMOUNT)
    assert cost.slippage_usd > 0.0
    assert cost.bridge_usd == 0.0


def test_toute_action_du_moteur_a_une_description_physique() -> None:
    """Une action absente de la table coûterait zéro en silence."""
    assert set(get_args(ActionKind)) == set(LEGS)


def test_une_action_inconnue_refuse_plutot_que_de_couter_zero() -> None:
    with pytest.raises(KeyError, match="sans description physique"):
        charge("TELEPORTATION", DEFAULT, eth_price=ETH)  # type: ignore[arg-type]
