"""Le moteur : la frise, le bilan et le VRAI moteur de décision du bot.

Ce qui tourne ici n'est pas une imitation de la boucle de production : c'est
`delta0.decision.decide`, la fonction que le bot exécute, nourrie de `Snapshot`
construits par le grand livre. Un backtest qui réimplémenterait la table de
décision mesurerait sa propre copie, et la copie serait juste le jour où on
l'écrit, puis fausse.

Quatre choix font tout le reste :

**On regarde la minute en quatre points, pas un.** Ouverture, plus haut, plus
bas, clôture : une mèche liquide aussi bien qu'une clôture (README §15.3), et
ne lire que la clôture effacerait précisément les instants qui tuent. L'ordre
des deux extrêmes À L'INTÉRIEUR d'une minute est inconnu — l'OHLC ne le dit pas
— et cela ne change rien à ce qui compte : les deux sont testés contre les
seuils. Cela ne changerait la suite que si une action lancée au premier extrême
pouvait atterrir avant le second, ce que les latences mesurées (1 s à 316 s)
rendent rare et que le journal permet de compter.

**Les actions atterrissent en retard, et le prix continue de courir.** Les
latences viennent du run M1, pas d'un souhait. C'est la seule façon de répondre
à la question que le classeur ne sait pas poser : une pompe de huit minutes
traverse-t-elle un krach de deux ?

**La liquidation se guette des DEUX côtés.** Aave tue par le bas quand le
facteur de santé passe sous 1 ; Hyperliquid tue par le haut quand le ratio de
marge passe sous la marge de maintenance. Un moteur qui ne surveille que le
premier — comme le harnais 1.6 le faisait — déclare sain un montage dont la
jambe short a déjà été fermée par la place.

**L'urgence ne préempte rien, par défaut.** Le README §6 dit que « les urgences
préemptent tout » ; c'est une phrase, pas du code. Aujourd'hui l'ordonnanceur
attend chaque action, donc une lente tient le sol : un re-centrage de 316 s
empêche P2 de seulement DÉCIDER pendant cinq minutes. `preempt=True` mesure ce
que le chantier 3.5 achèterait, et le premier tirage réel dit que ce n'est pas
un confort.

**Une campagne qui liquide s'arrête.** Le critère du README est « zéro
liquidation avec la pompe au p95 » : un run qui liquide a déjà répondu. Ce
qu'une liquidation LAISSE derrière elle — collatéral saisi, prime du
liquidateur, marge perdue — n'est pas encore modélisé, et continuer sur un
bilan inventé rendrait des chiffres d'après-liquidation qui ne veulent rien
dire.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from backtest import ledger
from backtest.cache import Month
from backtest.costs import DEFAULT, Charge, Costs
from backtest.ledger import Book, Moment
from backtest.timeline import FundingEvent, Minute, Segment, Timeline
from delta0.config import Config
from delta0.decision import BlindState, OperationalContext, decide
from delta0.types import Action, ActionKind, Snapshot

HOUR_MS = 3_600_000
HOURS_PER_YEAR = 24 * 365
DAY_MS = 86_400_000
FUNDING_WINDOW_MS = 30 * DAY_MS

# L'ordre de lecture d'une minute. Les extrêmes passent avant la clôture : ce
# sont eux qui franchissent les seuils, et une décision prise à la clôture
# arriverait une minute après le danger.
MOMENTS: tuple[Moment, ...] = (Moment.OPEN, Moment.HIGH, Moment.LOW, Moment.CLOSE)

# Secondes entre une décision et son effet, p95 du run M1 (voir tests/simulator.py
# et memory/). STEPWISE_DELEVERAGE n'a jamais été mesuré : 60 s est un
# marque-place délibérément optimiste, signalé comme tel plutôt que tu.
LATENCY_S: dict[ActionKind, float] = {
    "NOOP": 0.0,
    "ADD_ISOLATED_MARGIN": 1.0,
    "REDUCE": 1.0,
    "REPAY_FROM_CUSHION": 1.3,
    "STEPWISE_DELEVERAGE": 60.0,
    "PUMP_UP": 11.2,
    "PUMP_DOWN": 316.0,
    "RETRUE_SHORT": 1.0,
    "RECENTER_UP": 316.0,
    "RECENTER_DOWN": 316.0,
    "LIQUIDATION_RESPONSE": 1.0,
    "SKIM_RECOMPOSE": 316.0,
    "REGIME_STEP": 316.0,
}


class Venue(StrEnum):
    """Qui a tué la position."""

    AAVE = "Aave"
    HYPERLIQUID = "Hyperliquid"


@dataclass(frozen=True, slots=True)
class Done:
    """Une action posée : quand elle a été décidée, quand elle a atterri, ce qu'elle a coûté."""

    decided_ms: int
    landed_ms: int
    kind: ActionKind
    reason: str
    charge: Charge
    moved_usd: float
    note: str
    segment: Segment


@dataclass(frozen=True, slots=True)
class Liquidation:
    """Ce qui a tué le montage, et où."""

    ts_ms: int
    venue: Venue
    hf: float
    margin_ratio: float
    segment: Segment


@dataclass(slots=True)
class Journal:
    """Ce que la campagne a fait, sans agrégat prématuré.

    Les totaux se calculent après coup et par segment ; garder les événements
    permet à un rapport de poser une question qu'on n'avait pas prévue. Une
    campagne de cinq ans en produit quelques milliers, pas des millions : ce
    sont les DÉCISIONS qui sont rares, pas les minutes.
    """

    minutes: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    done: list[Done] = field(default_factory=list)
    liquidation: Liquidation | None = None
    funding_received_usd: float = 0.0
    interest_paid_usd: float = 0.0
    hf_min: float = float("inf")
    margin_ratio_min: float = float("inf")
    preempted: int = 0

    @property
    def survived(self) -> bool:
        return self.liquidation is None

    def count(self, kind: ActionKind) -> int:
        return sum(1 for entry in self.done if entry.kind == kind)

    def charged(self) -> Charge:
        total = Charge()
        for entry in self.done:
            total = total + entry.charge
        return total


@dataclass(slots=True)
class Funding30d:
    """La moyenne de funding sur trente jours, celle que la porte de régime lit.

    Les taux se SOMMENT sur la fenêtre, et se divisent par les heures que ces
    versements DÉCLARENT couvrir — pas par l'écart entre le premier et le
    dernier, qui vaut une période de moins et gonflerait la moyenne de 4 % sur
    une journée. Convertir chaque ligne en taux horaire avant de moyenner
    reviendrait au même calcul en moins lisible, et ferait croire que la
    conversion est tranchée alors que la phase 8 h d'Hyperliquid ne l'est pas.
    """

    window: deque[tuple[int, float, int]] = field(default_factory=deque)
    total: float = 0.0
    hours: int = 0

    def add(self, event: FundingEvent) -> None:
        self.window.append((event.ts_ms, event.rate, event.interval_hours))
        self.total += event.rate
        self.hours += event.interval_hours

    def annualized(self, now_ms: int) -> float:
        while self.window and self.window[0][0] < now_ms - FUNDING_WINDOW_MS:
            _, rate, hours = self.window.popleft()
            self.total -= rate
            self.hours -= hours
        if not self.window or self.hours <= 0:
            return 0.0
        return self.total / self.hours * HOURS_PER_YEAR


@dataclass
class Engine:
    """Une campagne : une frise, un bilan de départ, et le moteur du bot."""

    config: Config
    costs: Costs = DEFAULT
    one_in_flight: bool = True
    preempt: bool = False
    anchor_price: float | None = None
    journal: Journal = field(default_factory=Journal)
    _pending: list[tuple[int, Action]] = field(default_factory=list)
    _funding: Funding30d = field(default_factory=Funding30d)
    _last_funding: FundingEvent | None = None
    _last_skim: datetime | None = None

    def run(self, timeline: Timeline, book: Book, start: Month, end: Month) -> Journal:
        for minute in timeline.walk(start, end):
            self._minute(minute, book)
            if self.journal.liquidation is not None:
                break
        return self.journal

    # --- une minute ------------------------------------------------------------

    def _minute(self, minute: Minute, book: Book) -> None:
        self.journal.minutes += 1
        if self.journal.first_ms is None:
            self.journal.first_ms = minute.ts_ms
        self.journal.last_ms = minute.ts_ms

        if self.anchor_price is None:
            # L'ancre de re-centrage part du premier prix vu, comme après un
            # BUILD. La laisser à None ferait dormir P7 sur toute la campagne,
            # et un backtest sans re-centrage ne mesure plus le montage.
            self.anchor_price = ledger.prices(minute, Moment.OPEN)[1]

        self.journal.interest_paid_usd += ledger.accrue_debt(book, minute.borrow_factor)
        self._land(minute, book)
        if minute.funding is not None:
            self._settle(minute, book)

        decided = False
        for moment in MOMENTS:
            observed = self._observe(book, minute, moment)
            if self._dead(observed, minute):
                return
            if decided or (self.one_in_flight and self._pending and not self.preempt):
                continue
            action = decide(observed, self.config, self._context(minute, observed))
            if action.kind == "NOOP":
                continue
            if self.preempt and self._pending:
                in_flight = min(pending.priority for _, pending in self._pending)
                if action.priority >= in_flight:
                    continue
                # Strictement plus urgente : elle prend la place de ce qui volait.
                self._pending.clear()
                self.journal.preempted += 1
            self._pending.append((minute.ts_ms + self._delay_ms(action), action))
            decided = True

    def _settle(self, minute: Minute, book: Book) -> None:
        """Le versement tombe à l'ouverture de sa minute : il est daté à l'heure ronde."""
        event = minute.funding
        assert event is not None
        _, mark = ledger.prices(minute, Moment.OPEN)
        self.journal.funding_received_usd += ledger.settle_funding(book, event, mark)
        self._funding.add(event)
        self._last_funding = event

    def _land(self, minute: Minute, book: Book) -> None:
        """Poser les actions dont la latence est écoulée, à l'ouverture de la minute."""
        still: list[tuple[int, Action]] = []
        for ready_ms, action in self._pending:
            if ready_ms > minute.ts_ms:
                still.append((ready_ms, action))
                continue
            applied = ledger.apply(action, book, minute, Moment.OPEN, self.costs, self.config)
            if action.kind in ("RECENTER_UP", "RECENTER_DOWN"):
                # L'ancre suit le re-centrage : sans cela P7 se re-déclencherait
                # à chaque cycle sur le même écart, déjà corrigé.
                self.anchor_price = ledger.prices(minute, Moment.OPEN)[1]
            if action.kind == "SKIM_RECOMPOSE":
                self._last_skim = datetime.fromtimestamp(minute.ts_ms / 1000, UTC)
            self.journal.done.append(
                Done(
                    decided_ms=ready_ms - self._delay_ms(action),
                    landed_ms=minute.ts_ms,
                    kind=action.kind,
                    reason=action.reason,
                    charge=applied.charge,
                    moved_usd=applied.moved_usd,
                    note=applied.note,
                    segment=minute.segment,
                )
            )
        self._pending = still

    def _observe(self, book: Book, minute: Minute, moment: Moment) -> Snapshot:
        last = self._last_funding
        return ledger.snapshot(
            book,
            minute,
            moment,
            # Reporting seulement : la table de décision ne lit que la moyenne
            # 30 jours. Un taux ramené à l'heure sur une ligne de 8 h serait une
            # conversion, et les conversions se tranchent au gel de la méthode.
            funding_last_hour=0.0 if last is None else last.rate / last.interval_hours,
            funding_30d_annualized=self._funding.annualized(minute.ts_ms),
            maintenance_margin=self.config.maintenance_margin,
            ltv_max=ledger.LTV_MAX_TODAY,
        )

    def _context(self, minute: Minute, observed: Snapshot) -> OperationalContext:
        return OperationalContext(
            now_utc=observed.ts,
            # Le rejeu long ne coupe jamais le lien : le chaos est le travail
            # du §15.4, et un mode dégradé permanent mesurerait le watchdog.
            blind_state=BlindState.NOMINAL,
            anchor_price=self.anchor_price,
            # None tant que rien n'a été écrémé : le premier créneau hebdomadaire
            # s'ouvre alors normalement. Poser « maintenant » fermerait P9 pour
            # toute la campagne sans que rien ne le dise.
            last_skim_at=self._last_skim,
            # Porte de régime NEUTRE : exposition voulue = exposition tenue, donc
            # P10 ne se déclenche pas. C'est le côté « porte OFF » de l'A/B du
            # chantier 7.4 ; l'évaluateur de régime, qui lit la moyenne 30 jours
            # avec hystérésis, y sera branché ici.
            current_exposure_mult=self.config.exposure_mult,
            desired_exposure_mult=self.config.exposure_mult,
        )

    def _dead(self, observed: Snapshot, minute: Minute) -> bool:
        """Les deux façons de mourir, guettées à chaque point de la minute."""
        self.journal.hf_min = min(self.journal.hf_min, observed.hf)
        if observed.notional_usd > 0:
            self.journal.margin_ratio_min = min(
                self.journal.margin_ratio_min, observed.margin_ratio
            )
        venue: Venue | None = None
        if observed.hf <= 1.0:
            venue = Venue.AAVE
        elif observed.notional_usd > 0 and observed.margin_ratio <= self.config.maintenance_margin:
            venue = Venue.HYPERLIQUID
        if venue is None:
            return False
        self.journal.liquidation = Liquidation(
            ts_ms=minute.ts_ms,
            venue=venue,
            hf=observed.hf,
            margin_ratio=observed.margin_ratio,
            segment=minute.segment,
        )
        return True

    def _delay_ms(self, action: Action) -> int:
        return int(LATENCY_S.get(action.kind, 1.0) * 1000)
