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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from backtest.binance import Candle
from backtest.costs import BPS, Charge, Costs, charge
from backtest.timeline import FundingEvent, Minute
from delta0.config import Config
from delta0.decision import target_state
from delta0.types import Action, ActionKind, Snapshot, equity_usd

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
    # La flotte de gaz, en ETH. Elle vit hors du montage (README §4) mais elle
    # s'epuise : le run M1 a fini avec une flotte entamee et personne ne l'avait
    # prevu. La tenir ici rend le garde-fou `gas_min_eth` du bot rejouable, et
    # une campagne qui manque de gaz devient un resultat au lieu d'une surprise.
    gas_eth: float
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


def market_price(eth_price: float, ratio: float, steth_market: float | None) -> float:
    """Ce qu'un vendeur de wstETH encaisse vraiment. Trois étages, pas deux.

        wstETH -> stETH   au taux de conversion Lido, exact et rachetable
        stETH  -> ETH     au prix du MARCHÉ, 0,935 au plus bas du 2022-06-18
        ETH    -> USD     au comptant

    Aave suppose la parité stETH/ETH et saute donc le deuxième étage : c'est
    exactement pourquoi un décrochage ne liquide pas. Il coûte à la VENTE, et
    c'est ici qu'il se paie — un désendettement d'urgence en juin 2022 aurait
    vendu 6,5 % moins cher que ce que le facteur de santé laissait croire.

    Avant le 2021-08-25 le flux ne publie pas, et rien ne se vend : poser 1,0
    reviendrait à affirmer une parité que personne n'a observée.
    """
    if steth_market is None:
        raise ValueError(
            "aucun prix de marché stETH/ETH à cet instant — le flux Chainlink "
            "commence le 2021-08-25 ; une sortie ne se price pas sans lui"
        )
    return eth_price * ratio * steth_market


def exit_discount(steth_market: float | None) -> float:
    """Ce que la sortie perd face à l'oracle, en proportion. Zéro à la parité."""
    if steth_market is None:
        return 0.0
    return max(0.0, 1.0 - steth_market)


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
        gas_eth=book.gas_eth,
        ws_last_tick_age_s=LIVE_FEED,
        rpc_ok=True,
    )


# --- Ce qu'une action fait au bilan, et ce qu'elle paie pour le faire -----------

# Les actions qui REPLACENT le bilan entier, par opposition à celles qui
# l'ajustent. Elles passent toutes par le solveur de cible et par le même effet :
# le bot les distingue par ce qui les déclenche, pas par ce qu'elles font. Le
# rapport, lui, veut pouvoir les compter à part, parce que ce sont elles qui
# paient un échange, un pont et un ordre à chaque fois.
REBALANCING: frozenset[ActionKind] = frozenset(
    {"RECENTER_UP", "RECENTER_DOWN", "SKIM_RECOMPOSE", "REGIME_STEP"}
)


@dataclass(frozen=True, slots=True)
class World:
    """Ce qu'un effet a besoin de savoir du monde, en un seul paramètre.

    Les deux prix sont lus UNE fois, au moment demandé, et voyagent ensemble :
    un effet qui relirait la bougie pourrait la relire à un autre moment, et
    deux moments dans la même action feraient un monde qui n'a pas existé.
    """

    minute: Minute
    moment: Moment
    eth: float
    mark: float
    costs: Costs
    config: Config


@dataclass(frozen=True, slots=True)
class Applied:
    """Ce qu'une action a vraiment fait, et ce qu'elle a coûté.

    `moved_usd` est ce qui a bougé pour de bon, pas ce qui était demandé : une
    action bornée par un solde a fait moins, et `note` dit pourquoi. C'est ce
    qui distingue « la défense a joué » de « la défense a essayé ».

    Les frais, le glissement et le pont sont DÉJÀ retranchés des soldes ; ils
    figurent dans `charge` pour le rapport, pas pour être soustraits une
    seconde fois. Le gaz, lui, sort de la flotte en ETH.
    """

    kind: ActionKind
    charge: Charge
    moved_usd: float = 0.0
    note: str = ""


def _burn_gas(book: Book, spent: Charge, eth_price: float) -> None:
    """Retirer le gaz de la flotte. Une flotte vide ne bloque pas : elle se voit."""
    if spent.gas_usd > 0.0 and eth_price > 0.0:
        book.gas_eth -= spent.gas_usd / eth_price


def _resize_short(book: Book, target_eth: float, mark: float) -> float:
    """Porter le short à `target_eth`. Rend le notionnel échangé.

    Réduire réalise le résultat de la SEULE part fermée et laisse le prix
    d'entrée du reste intact ; agrandir moyenne l'entrée. Réaliser tout le
    résultat à chaque re-dimensionnement — ce que fait le harnais 1.6 pour
    rester lisible — ferait apparaître des gains que la place n'a pas versés.
    """
    target = max(0.0, target_eth)
    traded = abs(target - book.short_eth)
    if traded == 0.0:
        return 0.0
    if target < book.short_eth:
        closed = book.short_eth - target
        book.margin_usd += closed * (book.short_entry_px - mark)
    else:
        added = target - book.short_eth
        book.short_entry_px = (book.short_eth * book.short_entry_px + added * mark) / target
    book.short_eth = target
    return traded * mark


def _add_margin(book: Book, action: Action, world: World) -> Applied:
    """P2, la seule défense rapide du flanc haut : un appel local, gratuit."""
    asked = float(action.params["add_margin_amount_usdc"])
    amount = min(asked, book.hl_free_usdc)
    book.margin_usd += amount
    book.hl_free_usdc -= amount
    return Applied(
        kind="ADD_ISOLATED_MARGIN",
        charge=Charge(),
        moved_usd=amount,
        note="" if amount >= asked else "réserve épuisée",
    )


def _repay_cushion(book: Book, action: Action, world: World) -> Applied:
    asked = float(action.params["repay_amount_usdc"])
    amount = min(asked, book.cushion_usd, book.debt_usd)
    book.debt_usd -= amount
    book.cushion_usd -= amount
    spent = charge("REPAY_FROM_CUSHION", world.costs, eth_price=world.eth)
    _burn_gas(book, spent, world.eth)
    return Applied(
        kind="REPAY_FROM_CUSHION",
        charge=spent,
        moved_usd=amount,
        note="" if amount >= asked else "coussin insuffisant",
    )


def _pump_up(book: Book, action: Action, world: World) -> Applied:
    """Emprunter, ponter, ajouter en marge.

    Ce qui arrive est ce qui reste après le pont : le facturer au départ ferait
    croire qu'on a ajouté plus de marge qu'il n'en est arrivé.
    """
    amount = max(0.0, float(action.params["add_margin_amount_usdc"]))
    spent = charge("PUMP_UP", world.costs, eth_price=world.eth, bridged_usd=amount)
    book.debt_usd += amount
    book.margin_usd += max(0.0, amount - spent.bridge_usd)
    _burn_gas(book, spent, world.eth)
    return Applied(kind="PUMP_UP", charge=spent, moved_usd=amount)


def _pump_down(book: Book, action: Action, world: World) -> Applied:
    asked = float(action.params["repay_amount_usdc"])
    amount = min(asked, book.margin_usd)
    spent = charge("PUMP_DOWN", world.costs, eth_price=world.eth, bridged_usd=amount)
    book.margin_usd -= amount
    book.debt_usd -= min(book.debt_usd, max(0.0, amount - spent.bridge_usd))
    _burn_gas(book, spent, world.eth)
    return Applied(
        kind="PUMP_DOWN",
        charge=spent,
        moved_usd=amount,
        note="" if amount >= asked else "marge insuffisante",
    )


def _retrue(book: Book, action: Action, world: World) -> Applied:
    """Re-dimensionner le short. P1 traverse le carnet, le re-truage poste (README §9.1)."""
    maker = action.kind == "RETRUE_SHORT"
    traded = _resize_short(book, float(action.params["target_short_size_eth"]), world.mark)
    spent = charge(action.kind, world.costs, eth_price=world.eth, order_usd=traded, maker=maker)
    book.margin_usd -= spent.fee_usd
    _burn_gas(book, spent, world.eth)
    return Applied(kind=action.kind, charge=spent, moved_usd=traded)


def _nothing(book: Book, action: Action, world: World) -> Applied:
    return Applied(kind=action.kind, charge=Charge())


def _reduce(book: Book, action: Action, world: World) -> Applied:
    """P2 de repli : fermer une part du short quand la réserve est vide.

    Limitation de dégâts, pas sauvetage : la place rend la marge au prorata de
    la taille fermée, donc le ratio marge/notionnel ne bouge pas et le prix de
    liquidation non plus (mesuré le 2026-09-08, -0,012 %). Ce que cette branche
    doit rendre exact, c'est ce que la fermeture RÉALISE.
    """
    fraction = min(1.0, max(0.0, float(action.params["close_fraction"])))
    closed_eth = book.short_eth * fraction
    posted = book.margin_usd * fraction
    realized = closed_eth * (book.short_entry_px - world.mark)
    traded = _resize_short(book, book.short_eth - closed_eth, world.mark)
    # `_resize_short` a déjà porté le résultat réalisé à la marge ; ici on ne
    # libère que la marge POSTÉE, celle que la place rend au prorata.
    book.margin_usd -= posted
    book.hl_free_usdc += posted
    spent = charge("REDUCE", world.costs, eth_price=world.eth, order_usd=traded, maker=False)
    book.margin_usd -= spent.fee_usd
    _burn_gas(book, spent, world.eth)
    return Applied(
        kind="REDUCE",
        charge=spent,
        moved_usd=traded,
        note=f"résultat réalisé {realized:+.0f} $",
    )


def _deleverage(book: Book, action: Action, world: World) -> Applied:
    """P4 : vendre du collatéral pour rembourser, jusqu'à la cible.

    Deux prix cohabitent ici, et c'est tout l'intérêt de cette branche. L'excédent
    à rembourser se mesure au prix ORACLE, parce que c'est la vue d'Aave qui
    décide du LTV. Ce qu'on encaisse en vendant se mesure au prix du MARCHÉ, avec
    la décote du stETH s'il y en a une. En juin 2022 il aurait donc fallu vendre
    nettement plus de collatéral que le facteur de santé ne le laissait attendre.
    """
    oracle = oracle_price(world.eth, world.minute.ratio)
    target_ltv = float(action.params["target_ltv_after"])
    collateral = book.wsteth * oracle + book.cushion_usd
    excess = book.debt_usd - target_ltv * collateral
    if excess <= 0.0 or book.wsteth <= 0.0:
        return Applied(kind="STEPWISE_DELEVERAGE", charge=Charge(), note="rien à vendre")

    sale = market_price(world.eth, world.minute.ratio, world.minute.steth_market)
    kept = 1.0 - (world.costs.swap_fee.value + world.costs.swap_slippage.value) * BPS
    if kept <= 0.0:
        raise ValueError("frais d'échange au-delà de 100 % : barème incohérent")
    wanted = excess / (sale * kept)
    sold = min(wanted, book.wsteth)
    gross = sold * sale

    spent = charge("STEPWISE_DELEVERAGE", world.costs, eth_price=world.eth, swapped_usd=gross)
    proceeds = max(0.0, gross - spent.fee_usd - spent.slippage_usd)
    exhausted = sold >= book.wsteth
    book.wsteth -= sold
    book.debt_usd -= min(book.debt_usd, proceeds)
    _burn_gas(book, spent, world.eth)
    return Applied(
        kind="STEPWISE_DELEVERAGE",
        charge=spent,
        moved_usd=proceeds,
        note="collatéral épuisé" if exhausted else "",
    )


def _rebalance(book: Book, action: Action, world: World) -> Applied:
    """Re-dimensionner tout le montage sur la cible du solveur du bot.

    Les quatre actions qui passent ici — les deux re-centrages, l'écrémage et le
    pas de régime — font la même chose : elles ramènent le bilan au point fixe
    que `target_state` calcule pour l'équité du moment. Le bot les distingue par
    ce qui les DÉCLENCHE, pas par ce qu'elles font, et les écrire quatre fois
    ferait quatre occasions de diverger.

    Trois choix comptent ici, et aucun n'est neutre :

    **La cible se résout sur l'équité APRÈS coûts.** Un re-centrage qui viserait
    l'équité d'avant se retrouverait, une fois les frais payés, au-dessus de sa
    propre cible de LTV — et il recommencerait. On estime donc le coût sur les
    montants d'une première passe, on le retire de l'équité, puis on résout pour
    de bon. L'écart entre les deux passes est de l'ordre du coût lui-même
    (quelques dizaines de points de base des montants déplacés), et une
    troisième passe le diviserait encore par cent : elle ne vaut pas sa
    complexité, mais le principe — viser ce qu'on aura, pas ce qu'on a — n'est
    pas une approximation, c'est la correction d'un biais.

    **Le collatéral s'achète et se vend au prix du MARCHÉ, la cible s'exprime au
    prix ORACLE.** Le bot dimensionne sur ce qu'il observe, c'est-à-dire sur
    l'oracle ; le carnet, lui, sert au prix du marché. En période de décrochage
    les deux diffèrent, et c'est exactement le moment où un re-centrage descendant
    doit vendre.

    **Le short vise l'équivalent ETH du collatéral, pas le notionnel en dollars.**
    La neutralité est une égalité de quantités d'ETH (README §5) ; diviser un
    notionnel en dollars par le mark ré-introduirait le mélange de deux prix que
    la base ETH existe pour éviter.
    """
    equity = _equity(book, world)
    try:
        first = _legs(book, world, equity)
        estimate = _rebalance_charge(world, first)
        target = _legs(book, world, equity - estimate.total_usd)
    except ValueError as refus:
        # Le solveur refuse une équité qui ne laisse rien à déployer. Un montage
        # réduit à son coussin ne se recentre pas : il se constate.
        return Applied(kind=action.kind, charge=Charge(), note=f"cible insoluble : {refus}")
    spent = _rebalance_charge(world, target)
    _settle(book, world, target, spent)
    return Applied(
        kind=action.kind,
        charge=spent,
        moved_usd=target.swapped_usd + target.bridged_usd,
        note=f"spot {target.spot_usd:,.0f} $, short {target.short_eth:.3f} ETH",
    )


@dataclass(frozen=True, slots=True)
class Legs:
    """L'état visé, et ce qu'il faut déplacer pour l'atteindre."""

    wsteth: float
    spot_usd: float
    debt_usd: float
    short_eth: float
    margin_usd: float
    reserve_usd: float
    swapped_usd: float
    bridged_usd: float
    order_usd: float


def _equity(book: Book, world: World) -> float:
    """L'équité, par la formule du bot et pas une seconde.

    `equity_usd` de `delta0.types` est la même que celle dont le solveur se sert ;
    en réécrire une ici rouvrirait exactement l'écart que le panneau `status`
    avait affiché en annonçant 0,00 $ avec 169,80 $ en caisse.
    """
    oracle = oracle_price(world.eth, world.minute.ratio)
    return equity_usd(
        collateral_usd=book.wsteth * oracle + book.cushion_usd,
        isolated_margin_usd=book.effective_margin(world.mark),
        wallet_usdc=book.wallet_usdc,
        hl_free_usdc=book.hl_free_usdc,
        debt_usd=book.debt_usd,
    )


def _legs(book: Book, world: World, equity: float) -> Legs:
    """Le point fixe du solveur pour cette équité, et les montants à déplacer."""
    oracle = oracle_price(world.eth, world.minute.ratio)
    target = target_state(equity, world.config, cushion_usd=book.cushion_usd)

    wsteth = target.spot_target_usd / oracle
    short_eth = wsteth * world.minute.ratio  # neutralité en ETH, pas en dollars
    traded_wsteth = abs(wsteth - book.wsteth)
    # Le prix de vente n'est demandé que s'il y a quelque chose à échanger :
    # avant le 2021-08-25 il n'existe pas, et un re-centrage qui ne touche pas
    # au collatéral n'a pas à en dépendre.
    sale = (
        market_price(world.eth, world.minute.ratio, world.minute.steth_market)
        if traded_wsteth > 0.0
        else 0.0
    )
    return Legs(
        wsteth=wsteth,
        spot_usd=target.spot_target_usd,
        debt_usd=target.debt_target_usd,
        short_eth=short_eth,
        margin_usd=target.margin_target_usd,
        reserve_usd=target.reserve_target_usd,
        swapped_usd=traded_wsteth * sale,
        bridged_usd=abs(
            (target.margin_target_usd + target.reserve_target_usd)
            - (book.margin_usd + book.hl_free_usdc)
        ),
        order_usd=abs(short_eth - book.short_eth) * world.mark,
    )


def _rebalance_charge(world: World, legs: Legs) -> Charge:
    """Ce que ces jambes coûtent. Le re-centrage est planifié, donc il poste."""
    return charge(
        "RECENTER_UP",
        world.costs,
        eth_price=world.eth,
        swapped_usd=legs.swapped_usd,
        bridged_usd=legs.bridged_usd,
        order_usd=legs.order_usd,
        maker=True,
    )


def _settle(book: Book, world: World, legs: Legs, spent: Charge) -> None:
    """Poser l'état visé sur le bilan.

    Les postes s'écrivent, parce que c'est ce qu'un re-centrage FAIT : il replace
    le bilan, il ne l'ajuste pas.

    Le prix d'entrée du short repart au mark du moment, et ce n'est pas un
    détail : le résultat latent de l'ancienne position a déjà été compté dans
    l'équité, donc dans la cible. Le laisser courir sur la position le compterait
    une seconde fois, et un montage qui se recentre souvent accumulerait un gain
    imaginaire proportionnel au nombre de re-centrages.
    """
    book.short_eth = legs.short_eth
    book.short_entry_px = world.mark
    book.wsteth = legs.wsteth
    book.debt_usd = legs.debt_usd
    book.margin_usd = legs.margin_usd
    book.hl_free_usdc = legs.reserve_usd
    _burn_gas(book, spent, world.eth)


Effect = Callable[[Book, Action, World], Applied]

# Chaque action du moteur a un effet nommé sur le bilan. Une action absente de
# cette table refuse : ne rien faire en silence est la seule issue interdite.
EFFECTS: dict[ActionKind, Effect] = {
    "NOOP": _nothing,
    "ADD_ISOLATED_MARGIN": _add_margin,
    "REDUCE": _reduce,
    "REPAY_FROM_CUSHION": _repay_cushion,
    "STEPWISE_DELEVERAGE": _deleverage,
    "PUMP_UP": _pump_up,
    "PUMP_DOWN": _pump_down,
    "RETRUE_SHORT": _retrue,
    "LIQUIDATION_RESPONSE": _retrue,
    # Les quatre qui replacent tout le montage. Le bot les distingue par ce qui
    # les DÉCLENCHE, pas par ce qu'elles font.
    "RECENTER_UP": _rebalance,
    "RECENTER_DOWN": _rebalance,
    "SKIM_RECOMPOSE": _rebalance,
    "REGIME_STEP": _rebalance,
}


def apply(
    action: Action,
    book: Book,
    minute: Minute,
    moment: Moment,
    costs: Costs,
    config: Config,
) -> Applied:
    """Poser une action sur le bilan, coûts compris, bornée par ce qui est là.

    Chaque effet borne son montant par le solde qui le porte. Une action qui
    dépense plus qu'elle n'a est le genre d'erreur qui ne se voit jamais dans un
    total et qui rend un backtest optimiste exactement là où il ne faut pas :
    sur les chemins d'urgence, qui sont ceux qui manquent de tout.
    """
    effect = EFFECTS.get(action.kind)
    if effect is None:
        raise KeyError(f"action sans effet connu sur le bilan : {action.kind}")
    eth, mark = prices(minute, moment)
    return effect(book, action, World(minute, moment, eth, mark, costs, config))
