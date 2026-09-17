"""Chantier 7.3 — le grand livre : intérêt, funding, staking, et le monde qu'ils font.

Ce qui est défendu ici tient en quatre phrases, et chacune vaut un résultat
inversé si elle tombe :

- **le short encaisse un funding positif.** Une erreur de signe ne casserait
  rien et rendrait l'inverse exact de la thèse du montage ;
- **le staking ne crédite aucun jeton** : c'est le taux de conversion qui monte.
  En créditer serait compter le rendement deux fois ;
- **le décrochage du stETH n'entre pas dans le facteur de santé** — le prix
  oracle ne dépend que du taux de conversion et de l'ETH/USD ;
- **les deux bougies se lisent au même moment** : un plus haut de mark contre
  une clôture de spot fabriquerait un monde qui n'a jamais existé.
"""

from __future__ import annotations

import pytest

from backtest.binance import Candle
from backtest.ledger import (
    LTV_MAX_TODAY,
    Book,
    Moment,
    accrue_debt,
    funding_amount,
    health_factor,
    oracle_price,
    prices,
    settle_funding,
    snapshot,
)
from backtest.timeline import FundingEvent, Minute, Segment
from delta0.types import Snapshot

RATIO = 1.25
LT = 0.79
MARK = 2_500.0


def candle(open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(ts_ms=1_688_169_600_000, open=open_, high=high, low=low, close=close)


def minute(
    *,
    ratio: float = RATIO,
    eth: Candle | None = None,
    mark: Candle | None = None,
    borrow_apr: float = 0.05,
) -> Minute:
    return Minute(
        ts_ms=1_688_169_600_000,
        eth=eth if eth is not None else candle(2_500.0, 2_520.0, 2_480.0, 2_510.0),
        mark=mark if mark is not None else candle(2_501.0, 2_530.0, 2_470.0, 2_512.0),
        ratio=ratio,
        borrow_index=10**27,
        borrow_factor=1.0,
        borrow_apr=borrow_apr,
        reserve="usdc-arbitrum",
        segment=Segment.FIDELE,
    )


def book(**changed: float) -> Book:
    """Le châssis de référence : 16 wstETH, 20 ETH short, la feuille de tests/world.py."""
    base: dict[str, float] = {
        "wsteth": 16.0,
        "cushion_usd": 1_000.0,
        "debt_usd": 35_000.0,
        "short_eth": 20.0,
        "short_entry_px": MARK,
        "margin_usd": 5_000.0,
        "hl_free_usdc": 1_000.0,
        "wallet_usdc": 0.0,
        "lt": LT,
    }
    base.update(changed)
    return Book(**base)


# --- le funding, et son signe --------------------------------------------------


def test_le_short_encaisse_quand_le_taux_est_positif() -> None:
    """La thèse du montage entier. Ce test n'affirme rien d'autre, exprès."""
    paye = FundingEvent(ts_ms=0, rate=0.0001, interval_hours=1, venue="hyperliquid")
    assert funding_amount(paye, 50_000.0) == pytest.approx(5.0)


def test_le_short_paie_quand_le_taux_est_negatif() -> None:
    du = FundingEvent(ts_ms=0, rate=-0.0001, interval_hours=1, venue="hyperliquid")
    assert funding_amount(du, 50_000.0) == pytest.approx(-5.0)


def test_le_versement_va_a_la_marge_isolee() -> None:
    livre = book()
    recu = settle_funding(
        livre, FundingEvent(ts_ms=0, rate=0.0001, interval_hours=1, venue="hyperliquid"), MARK
    )
    assert recu == pytest.approx(20.0 * MARK * 0.0001)
    assert livre.margin_usd == pytest.approx(5_000.0 + recu)


def test_le_versement_se_mesure_sur_le_notionnel_du_moment() -> None:
    """Une position qui vaut plus paie sur ce qu'elle vaut, pas sur son prix d'entrée."""
    livre = book()
    event = FundingEvent(ts_ms=0, rate=0.0001, interval_hours=1, venue="hyperliquid")
    monte = settle_funding(book(), event, 5_000.0)
    repos = settle_funding(livre, event, MARK)
    assert monte == pytest.approx(2 * repos)


def test_la_periode_declaree_ne_se_reconvertit_pas() -> None:
    """Un taux 8 h s'applique une fois, tel quel. Le diviser puis le remultiplier
    est exactement la manœuvre qui fabrique un facteur 8."""
    huit = FundingEvent(ts_ms=0, rate=0.001, interval_hours=8, venue="hyperliquid")
    une = FundingEvent(ts_ms=0, rate=0.001, interval_hours=1, venue="hyperliquid")
    assert funding_amount(huit, 50_000.0) == funding_amount(une, 50_000.0)


# --- l'intérêt -----------------------------------------------------------------


def test_un_facteur_a_un_ne_coute_rien() -> None:
    livre = book()
    assert accrue_debt(livre, 1.0) == 0.0
    assert livre.debt_usd == 35_000.0


def test_l_interet_est_le_rapport_des_index() -> None:
    livre = book()
    couru = accrue_debt(livre, 1.0001)
    assert couru == pytest.approx(3.5)
    assert livre.debt_usd == pytest.approx(35_003.5)


def test_une_dette_qui_decroit_toute_seule_est_refusee() -> None:
    """Un index d'emprunt en baisse est une lecture corrompue, pas un cadeau d'Aave."""
    with pytest.raises(ValueError, match="ne décroît pas toute seule"):
        accrue_debt(book(), 0.9999)


# --- le staking et l'oracle ----------------------------------------------------


def test_le_staking_ne_credite_aucun_jeton_mais_leve_le_collateral() -> None:
    livre = book()
    avant = health_factor(livre, oracle_price(MARK, 1.25))
    apres = health_factor(livre, oracle_price(MARK, 1.26))
    assert livre.wsteth == 16.0  # aucun jeton crédité
    assert apres > avant


def test_le_prix_oracle_ne_depend_que_du_taux_et_de_l_eth() -> None:
    """Le décrochage du stETH n'entre pas dans le facteur de santé : il coûte à la sortie."""
    assert oracle_price(2_000.0, 1.25) == pytest.approx(2_500.0)
    assert oracle_price(2_000.0, 1.20) == pytest.approx(2_400.0)


def test_le_facteur_de_sante_compte_le_coussin_dans_le_collateral() -> None:
    livre = book()
    attendu = LT * (16.0 * oracle_price(MARK, RATIO) + 1_000.0) / 35_000.0
    assert health_factor(livre, oracle_price(MARK, RATIO)) == pytest.approx(attendu)


def test_sans_dette_le_facteur_de_sante_est_infini() -> None:
    assert health_factor(book(debt_usd=0.0), 3_000.0) == float("inf")


# --- la marge ------------------------------------------------------------------


def test_la_marge_effective_porte_le_resultat_latent() -> None:
    """Sans lui, le ratio de marge ne bougeait pas avec le prix et le flanc haut dormait."""
    livre = book()
    assert livre.effective_margin(MARK) == pytest.approx(5_000.0)
    assert livre.effective_margin(2_400.0) == pytest.approx(5_000.0 + 20 * 100.0)
    assert livre.effective_margin(2_600.0) == pytest.approx(5_000.0 - 20 * 100.0)


def test_la_marge_effective_ne_passe_pas_sous_zero() -> None:
    assert book().effective_margin(10_000.0) == 0.0


# --- la lecture de la minute ---------------------------------------------------


def test_les_deux_bougies_se_lisent_au_meme_moment() -> None:
    une = minute(
        eth=candle(100.0, 110.0, 90.0, 105.0),
        mark=candle(200.0, 220.0, 180.0, 210.0),
    )
    assert prices(une, Moment.OPEN) == (100.0, 200.0)
    assert prices(une, Moment.HIGH) == (110.0, 220.0)
    assert prices(une, Moment.LOW) == (90.0, 180.0)
    assert prices(une, Moment.CLOSE) == (105.0, 210.0)


# --- le pont vers le moteur du bot ---------------------------------------------


def observed(livre: Book, une: Minute, moment: Moment = Moment.CLOSE) -> Snapshot:
    return snapshot(
        livre,
        une,
        moment,
        funding_last_hour=1.25e-5,
        funding_30d_annualized=0.11,
        maintenance_margin=0.02,
        ltv_max=LTV_MAX_TODAY,
    )


def test_le_snapshot_laisse_le_bot_deriver_ses_grandeurs() -> None:
    """Aucune grandeur n'a deux définitions : le backtest ne pose que ce qu'il ne peut pas lire."""
    livre = book()
    une = minute(eth=candle(2_500.0, 2_500.0, 2_500.0, 2_500.0))
    vue = observed(livre, une)

    assert vue.wsteth_price_usd == pytest.approx(2_500.0 * RATIO)
    assert vue.spot_usd == pytest.approx(16.0 * 2_500.0 * RATIO)
    assert vue.collateral_usd == pytest.approx(vue.spot_usd + 1_000.0)
    assert vue.ltv == pytest.approx(35_000.0 / vue.collateral_usd)
    assert vue.hf == pytest.approx(health_factor(livre, vue.wsteth_price_usd))
    # 16 wstETH x 1,25 = 20 ETH équivalents face à 20 ETH de short : delta nul.
    assert vue.delta_eth == pytest.approx(0.0)


def test_le_snapshot_prend_le_mark_pour_la_jambe_perp() -> None:
    une = minute(
        eth=candle(2_500.0, 2_600.0, 2_400.0, 2_500.0),
        mark=candle(2_501.0, 2_700.0, 2_300.0, 2_502.0),
    )
    haut = observed(book(), une, Moment.HIGH)
    assert haut.mark_price == 2_700.0
    assert haut.wsteth_price_usd == pytest.approx(2_600.0 * RATIO)


def test_le_backtest_ne_simule_ni_coupure_ni_panne_rpc() -> None:
    """Couper le lien est le travail du chaos (§15.4), pas celui du rejeu long.

    Poser un flux périmé ou un RPC muet ferait passer le bot en mode dégradé sur
    toute la période, et le backtest mesurerait le watchdog au lieu des bandes.
    """
    vue = observed(book(), minute())
    assert vue.rpc_ok
    assert vue.ws_last_tick_age_s == 0.0
