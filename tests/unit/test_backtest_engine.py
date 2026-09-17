"""Chantier 7.3 — la boucle : le vrai moteur du bot, nourri d'une frise fabriquée.

Les minutes sont construites à la main plutôt que lues d'un cache : ce qui est
défendu ici n'est pas la lecture des archives — elle a ses tests — mais ce que
la boucle fait d'un chemin de prix donné. Un chemin écrit à la main dit
exactement où le danger tombe, ce qu'un mois réel ne dit jamais.

Quatre choses comptent :

- **une mèche déclenche comme une clôture.** Ne lire que la clôture effacerait
  les instants qui tuent ;
- **une action atterrit en retard**, et le prix court pendant ce temps. C'est la
  seule question que le classeur ne sait pas poser ;
- **on meurt des deux côtés** : Aave par le bas, Hyperliquid par le haut. Un
  moteur qui ne guette que le premier déclare sain un montage dont la jambe
  short a déjà été fermée ;
- **le funding encaissé, l'intérêt payé et les frais** se retrouvent dans le
  journal, séparés, parce que c'est d'eux que le rapport annuel sera fait.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from itertools import pairwise
from pathlib import Path

import pytest

from backtest.binance import MINUTE_MS, Candle
from backtest.costs import DEFAULT
from backtest.engine import LATENCY_S, Engine, Funding30d, Venue
from backtest.ledger import LT_TODAY, Book, Moment, prices
from backtest.timeline import FundingEvent, Minute, Segment
from delta0.config import load_config

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
RATIO = 1.25
START_MS = 1_688_169_600_000  # 2023-07-01T00:00:00Z, segment FIDÈLE


def flat(price: float) -> Candle:
    return Candle(ts_ms=0, open=price, high=price, low=price, close=price)


def wick(price: float, *, high: float | None = None, low: float | None = None) -> Candle:
    return Candle(
        ts_ms=0,
        open=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        close=price,
    )


def frise(
    candles: Sequence[Candle],
    *,
    borrow_factor: float = 1.0,
    funding: dict[int, FundingEvent] | None = None,
    steth_market: float | None = 1.0,
) -> list[Minute]:
    """Une frise où le comptant et le mark suivent le même chemin."""
    due = funding or {}
    return [
        Minute(
            ts_ms=START_MS + index * MINUTE_MS,
            eth=candle,
            mark=candle,
            ratio=RATIO,
            steth_market=steth_market,
            borrow_index=10**27,
            borrow_factor=borrow_factor,
            borrow_apr=0.05,
            reserve="usdc-arbitrum",
            segment=Segment.FIDELE,
            funding=due.get(index),
        )
        for index, candle in enumerate(candles)
    ]


class FakeTimeline:
    """Une frise déjà construite, servie telle quelle."""

    def __init__(self, minutes: Sequence[Minute]) -> None:
        self.minutes = minutes

    def walk(self, start: object, end: object) -> Iterator[Minute]:
        yield from self.minutes


def book(**changed: float) -> Book:
    """Le châssis de référence, au repos : LTV 0,686, delta nul."""
    base: dict[str, float] = {
        "wsteth": 16.0,
        "cushion_usd": 1_000.0,
        "debt_usd": 35_000.0,
        "short_eth": 20.0,
        "short_entry_px": 2_500.0,
        "margin_usd": 5_000.0,
        "hl_free_usdc": 1_000.0,
        "wallet_usdc": 0.0,
        "gas_eth": 0.05,
        "lt": LT_TODAY,
    }
    base.update(changed)
    return Book(**base)


def campaign(minutes: Sequence[Minute], livre: Book, **kwargs: object) -> Engine:
    engine = Engine(config=CONFIG, costs=DEFAULT, **kwargs)  # type: ignore[arg-type]
    engine.run(FakeTimeline(minutes), livre, (2023, 7), (2023, 7))  # type: ignore[arg-type]
    return engine


# --- la campagne au repos -------------------------------------------------------


def test_un_marche_plat_ne_declenche_rien() -> None:
    """Le bilan de référence est un point fixe : au repos, la table ne dit rien."""
    engine = campaign(frise([flat(2_500.0)] * 30), book())
    assert engine.journal.minutes == 30
    assert engine.journal.done == []
    assert engine.journal.survived


def test_l_interet_court_meme_quand_personne_n_agit() -> None:
    livre = book()
    engine = campaign(frise([flat(2_500.0)] * 10, borrow_factor=1.000001), livre)
    assert engine.journal.interest_paid_usd > 0.0
    assert livre.debt_usd > 35_000.0


def test_le_funding_encaisse_entre_au_journal_et_a_la_marge() -> None:
    livre = book()
    verse = FundingEvent(ts_ms=START_MS, rate=0.0001, interval_hours=1, venue="hyperliquid")
    engine = campaign(frise([flat(2_500.0)] * 5, funding={0: verse}), livre)
    attendu = 20.0 * 2_500.0 * 0.0001
    assert engine.journal.funding_received_usd == pytest.approx(attendu)
    assert livre.margin_usd == pytest.approx(5_000.0 + attendu)


# --- la mèche -------------------------------------------------------------------


def test_une_meche_declenche_comme_une_cloture() -> None:
    """Le plus haut de la minute passe le seuil, la clôture non : ça doit tirer."""
    calme = frise([flat(2_500.0)] * 3)
    mechee = frise([flat(2_500.0), wick(2_500.0, high=2_660.0), flat(2_500.0)])

    assert campaign(calme, book()).journal.done == []
    tire = campaign(mechee, book())
    assert tire.journal.done, "une mèche à +6,4 % doit réveiller la défense du flanc haut"


def test_la_defense_du_flanc_haut_est_l_ajout_de_marge() -> None:
    """P2 ajoute de la marge : c'est la seule chose qui bouge le prix de liquidation."""
    monte = frise([flat(2_500.0), wick(2_500.0, high=2_660.0), *[flat(2_500.0)] * 5])
    engine = campaign(monte, book())
    assert engine.journal.count("ADD_ISOLATED_MARGIN") >= 1


# --- la latence -----------------------------------------------------------------


def test_une_action_atterrit_apres_sa_latence_jamais_avant() -> None:
    """Le prix court pendant l'attente : c'est la question que le classeur ne pose pas."""
    chute = frise([flat(2_500.0), *[flat(2_200.0)] * 10])
    engine = campaign(chute, book())
    assert engine.journal.done, "une chute de 12 % doit faire jouer le coussin"
    for pose in engine.journal.done:
        assert pose.landed_ms >= pose.decided_ms + LATENCY_S[pose.kind] * 1000
        assert pose.landed_ms > pose.decided_ms, "rien n'atterrit dans la minute de sa décision"


def test_une_seule_action_en_vol_a_la_fois() -> None:
    """Invariant I7 : jamais deux exécutions en même temps.

    Sans lui, les quatre points de chaque minute empileraient quatre défenses,
    et la campagne dépenserait le coussin en un cycle.
    """
    chute = frise([flat(2_500.0), *[flat(2_200.0)] * 10])
    engine = campaign(chute, book())
    poses = sorted(engine.journal.done, key=lambda entry: entry.decided_ms)
    for avant, apres in pairwise(poses):
        assert apres.decided_ms >= avant.landed_ms


# --- les deux façons de mourir ---------------------------------------------------


def test_aave_tue_par_le_bas() -> None:
    effondrement = frise([flat(2_500.0), flat(1_500.0)])
    engine = campaign(effondrement, book())
    assert engine.journal.liquidation is not None
    assert engine.journal.liquidation.venue is Venue.AAVE
    assert not engine.journal.survived


def test_hyperliquid_tue_par_le_haut() -> None:
    """La marge isolée fond quand le prix monte ; le facteur de santé, lui, s'améliore.

    Un moteur qui ne guetterait qu'Aave déclarerait ce montage en pleine forme
    alors que la place a déjà fermé la jambe short.
    """
    squeeze = frise([flat(2_500.0), flat(2_800.0)])
    engine = campaign(squeeze, book(hl_free_usdc=0.0))
    assert engine.journal.liquidation is not None
    assert engine.journal.liquidation.venue is Venue.HYPERLIQUID
    assert engine.journal.liquidation.hf > 1.0, "Aave allait bien, et pourtant c'est mort"


def test_la_campagne_s_arrete_a_la_liquidation() -> None:
    """Continuer sur un bilan d'après-liquidation rendrait des chiffres qui ne veulent rien dire."""
    engine = campaign(frise([flat(2_500.0), flat(1_500.0), *[flat(2_500.0)] * 50]), book())
    assert engine.journal.minutes == 2


# --- la moyenne de funding sur trente jours -------------------------------------


def test_la_moyenne_30j_somme_les_taux_et_divise_par_les_heures() -> None:
    """Convertir chaque ligne en taux horaire choisirait ce que vaut sa période."""
    window = Funding30d()
    for hour in range(24):
        window.add(
            FundingEvent(
                ts_ms=START_MS + hour * 3_600_000,
                rate=0.00001,
                interval_hours=1,
                venue="hyperliquid",
            )
        )
    annualise = window.annualized(START_MS + 23 * 3_600_000)
    assert annualise == pytest.approx(0.00001 * 24 * 365, rel=1e-6)


def test_la_fenetre_oublie_ce_qui_a_plus_de_trente_jours() -> None:
    window = Funding30d()
    window.add(FundingEvent(ts_ms=START_MS, rate=0.01, interval_hours=1, venue="hyperliquid"))
    assert window.annualized(START_MS + 31 * 86_400_000) == 0.0


# --- ce que le journal garde -----------------------------------------------------


def test_le_journal_garde_les_couts_par_nature() -> None:
    """Un rapport doit pouvoir dire si l'année coûte par les frais ou par le glissement."""
    chute = frise([flat(2_500.0), *[flat(2_200.0)] * 10])
    engine = campaign(chute, book())
    total = engine.journal.charged()
    assert engine.journal.done
    assert total.gas_usd > 0.0, "quatre remboursements Aave, donc quatre transactions"
    assert total.total_usd == pytest.approx(
        total.fee_usd + total.slippage_usd + total.bridge_usd + total.gas_usd
    )


def test_le_journal_retient_le_pire_facteur_de_sante_vu() -> None:
    engine = campaign(frise([flat(2_500.0), wick(2_500.0, low=2_200.0)]), book())
    assert engine.journal.hf_min < LT_TODAY * (16 * 2_500 * RATIO + 1_000) / 35_000


def test_les_deux_bougies_se_lisent_au_meme_moment_dans_la_boucle() -> None:
    """Contrôle de cohérence : la boucle ne mélange pas deux moments d'une minute."""
    une = frise([wick(2_500.0, high=2_600.0, low=2_400.0)])[0]
    assert prices(une, Moment.HIGH) == (2_600.0, 2_600.0)
    assert prices(une, Moment.LOW) == (2_400.0, 2_400.0)
