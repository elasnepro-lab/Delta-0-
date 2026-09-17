"""Le grand livre : ce que le temps fait au bilan, avant toute décision.

Entre deux minutes, personne n'agit et pourtant trois choses bougent : la dette
grossit, le funding tombe, et le rendement du staking s'ajoute au collatéral.
Ce sont elles qui font le portage — le reste du backtest ne fait que défendre
ce que celles-ci produisent. Elles sont donc ici, séparées de tout le reste,
avec leurs conventions écrites.

**Le short ENCAISSE quand le taux de funding est positif.** C'est la thèse du
montage entier : sur les deux places, un taux positif fait payer les longs. Une
erreur de signe ici ne casserait rien, ne ferait échouer aucun test évident, et
rendrait exactement l'inverse du résultat cherché. D'où une fonction qui ne fait
que ça, et un test qui n'affirme rien d'autre.

**Le staking ne change aucun solde.** Le nombre de wstETH ne bouge pas ; c'est le
taux de conversion qui monte, donc la valeur du collatéral. Créditer des jetons
en plus serait un rendement compté deux fois.

**Le facteur de santé se calcule ici, faute de chaîne.** En production il est LU
sur Aave et jamais recalculé (README §9.2) ; un backtest n'a pas ce luxe. La
formule est donc à un seul endroit, et tout le reste — LTV, ratio de marge,
delta, équité — passe par les propriétés du `Snapshot` du bot, pour qu'aucune
grandeur n'ait deux définitions.

**Le seuil de liquidation historique n'est pas connu.** La gouvernance Aave l'a
bougé plusieurs fois ; on rejoue avec la valeur lue on-chain aujourd'hui
(0,7900 sur Arbitrum). Ce n'est pas anodin sur les segments anciens et c'est
marqué comme tel plutôt que passé sous silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from backtest.binance import Candle
from backtest.timeline import FundingEvent, Minute
from delta0.types import Snapshot

# Les paramètres Aave d'AUJOURD'HUI, appliqués au passé faute de mieux. Lus
# on-chain le 2026-09-08 (memory/aave_findings.md §9), rejouables par
# `scripts/read_aave_params.py` — qui reste la seule source pour le bot. La
# gouvernance les a bougés plusieurs fois et leur historique n'a pas été
# collecté : les appliquer à 2021 est une HYPOTHÈSE, pas une lecture, et elle
# pèse d'autant plus que le segment est ancien.
LT_TODAY = 0.79
LTV_MAX_TODAY = 0.75

# Ce que le backtest ne simule pas, et qui doit rester hors du chemin des
# décisions plutôt que d'y entrer par une valeur nulle : le flux de prix ne
# tombe jamais, le RPC répond toujours. Couper le lien est le travail du chaos
# (README §15.4), pas celui du rejeu long.
LIVE_FEED = 0.0
GAS_STOCKED_ETH = 1.0


class Moment(StrEnum):
    """Le point de la minute où l'on regarde le monde.

    Les bandes se testent contre les extrêmes, jamais contre les clôtures : une
    mèche liquide aussi bien qu'une clôture (README §15.3). Les deux bougies se
    lisent au MÊME moment — prendre le plus haut du mark avec la clôture du spot
    fabriquerait un monde où le perp s'envole pendant que le comptant dort.
    Rien ne dit que les deux extrêmes soient tombés à la même seconde ; c'est la
    lecture conjointe la moins inventive que l'OHLC permette.
    """

    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"


@dataclass(slots=True)
class Book:
    """Le bilan, tel qu'il se tient entre deux actions.

    `margin_usd` est la marge POSTÉE, à laquelle s'ajoute le résultat latent du
    short. Les tenir séparées est ce qui permet de dire ce qu'un re-dimensionnement
    réalise, et ce qu'un ajout de marge apporte vraiment.
    """

    wsteth: float
    cushion_usd: float
    debt_usd: float
    short_eth: float
    short_entry_px: float
    margin_usd: float
    hl_free_usdc: float
    wallet_usdc: float
    lt: float

    def unrealized_pnl(self, mark: float) -> float:
        """Un short gagne quand le prix baisse et saigne quand il monte."""
        return self.short_eth * (self.short_entry_px - mark)

    def effective_margin(self, mark: float) -> float:
        """La marge que la place voit : postée, plus le résultat latent.

        L'oublier fut l'angle mort du harnais 1.6 : sans elle le ratio de marge
        ne bougeait pas avec le prix, le flanc haut ne pouvait pas se déclencher,
        et la réserve paraissait inutile.
        """
        return max(0.0, self.margin_usd + self.unrealized_pnl(mark))


def price_at(candle: Candle, moment: Moment) -> float:
    """Le prix d'une bougie au moment demandé."""
    return {
        Moment.OPEN: candle.open,
        Moment.HIGH: candle.high,
        Moment.LOW: candle.low,
        Moment.CLOSE: candle.close,
    }[moment]


def prices(minute: Minute, moment: Moment) -> tuple[float, float]:
    """Le comptant et le mark, lus au même moment de la minute."""
    return price_at(minute.eth, moment), price_at(minute.mark, moment)


def accrue_debt(book: Book, factor: float) -> float:
    """Faire courir l'intérêt d'emprunt. Rend ce que la minute a coûté, en dollars.

    Le facteur est un rapport de deux `variableBorrowIndex` : l'intérêt
    réellement couru, sans convention de composition à choisir. Un facteur en
    dessous de 1 voudrait dire qu'Aave a rendu de l'argent aux emprunteurs, ce
    qui n'arrive pas ; il est refusé plutôt que soustrait.
    """
    if factor < 1.0:
        raise ValueError(f"la dette ne décroît pas toute seule : facteur {factor!r}")
    interest = book.debt_usd * (factor - 1.0)
    book.debt_usd += interest
    return interest


def funding_amount(event: FundingEvent, notional_usd: float) -> float:
    """Ce que le SHORT encaisse à ce versement. Négatif quand il paie.

    Le taux couvre la période déclarée par la ligne, pas une heure : il
    s'applique tel quel, une fois, à son instant. Le convertir en taux horaire
    pour le remultiplier ensuite serait l'occasion parfaite d'un facteur 8.
    """
    return event.rate * notional_usd


def settle_funding(book: Book, event: FundingEvent, mark: float) -> float:
    """Porter le versement à la marge isolée, où la place le règle.

    Le montant se mesure sur le notionnel du moment, pas sur celui de l'ouverture :
    une position qui a doublé de valeur paie sur ce qu'elle vaut.
    """
    received = funding_amount(event, book.short_eth * mark)
    book.margin_usd += received
    return received


def health_factor(book: Book, wsteth_price: float) -> float:
    """Le facteur de santé, calculé faute de pouvoir le lire sur la chaîne."""
    collateral = book.wsteth * wsteth_price + book.cushion_usd
    if book.debt_usd <= 0.0:
        return float("inf")
    return book.lt * collateral / book.debt_usd


def oracle_price(eth_price: float, ratio: float) -> float:
    """Le prix du wstETH que l'oracle Aave applique : le taux de conversion sur l'ETH/USD.

    Vérifié le 2026-09-16 (docs/backtest/verification-oracle-wsteth.md) : depuis
    le 2023-06-26 le prix de MARCHÉ du stETH n'y entre plus. Un décrochage ne
    liquide donc pas — il coûte à la sortie.
    """
    return eth_price * ratio


def snapshot(
    book: Book,
    minute: Minute,
    moment: Moment,
    *,
    funding_last_hour: float,
    funding_30d_annualized: float,
    maintenance_margin: float,
    ltv_max: float,
) -> Snapshot:
    """Le monde tel que le moteur de décision du bot le verra.

    Tout ce qui se dérive — LTV, ratio de marge, delta, équité — est laissé aux
    propriétés du `Snapshot`. Le backtest ne redéfinit que ce qu'il ne peut pas
    lire : le facteur de santé, et le prix oracle du wstETH.
    """
    eth, mark = prices(minute, moment)
    wsteth_price = oracle_price(eth, minute.ratio)
    return Snapshot(
        ts=datetime.fromtimestamp(minute.ts_ms / 1000, UTC),
        wsteth_atoken_balance=book.wsteth,
        wsteth_price_usd=wsteth_price,
        wsteth_eth_ratio=minute.ratio,
        usdc_atoken_balance=book.cushion_usd,
        usdc_variable_debt_balance=book.debt_usd,
        usdc_wallet_balance=book.wallet_usdc,
        hf=health_factor(book, wsteth_price),
        aave_lt_wsteth=book.lt,
        aave_ltv_max_wsteth=ltv_max,
        aave_emode=0,
        mark_price=mark,
        short_size_eth=book.short_eth,
        isolated_margin_usd=book.effective_margin(mark),
        hl_free_usdc=book.hl_free_usdc,
        hl_maintenance_margin=maintenance_margin,
        funding_last_hour=funding_last_hour,
        funding_30d_annualized=funding_30d_annualized,
        borrow_apr=minute.borrow_apr,
        gas_eth=GAS_STOCKED_ETH,
        ws_last_tick_age_s=LIVE_FEED,
        rpc_ok=True,
    )
