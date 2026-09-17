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
from delta0.decision import (
    BlindState,
    OperationalContext,
    Regime,
    decide,
    exposure_mult_of,
    regime_candidate,
    regime_step,
)
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
    regime_changes: list[tuple[int, float]] = field(default_factory=list)
    regime_suppressed: int = 0
    regime_rate_limited: int = 0

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
    regime: bool = False
    anchor_price: float | None = None
    journal: Journal = field(default_factory=Journal)
    _pending: list[tuple[int, Action]] = field(default_factory=list)
    _funding: Funding30d = field(default_factory=Funding30d)
    _last_funding: FundingEvent | None = None
    _last_skim: datetime | None = None
    _regime: Regime | None = None
    _regime_day: int | None = None
    _regime_step_ms: int | None = None

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
        # Après le versement, pas avant : la porte lit la moyenne 30 jours, et
        # l'évaluer d'abord la ferait décider sur une fenêtre à laquelle il
        # manque le versement de la minute même.
        self._evaluate_regime(minute)

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
            if action.kind == "REGIME_STEP" and not self._worth_stepping(action, observed, minute):
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
            # Porte OUVERTE : l'exposition tenue se mesure en inversant le
            # solveur, la voulue sort de l'évaluateur. Porte FERMÉE : les deux
            # sont égales, donc P10 se tait — c'est le côté « OFF » de l'A/B.
            current_exposure_mult=self._held(observed),
            desired_exposure_mult=self._wanted(observed),
        )

    def _held(self, observed: Snapshot) -> float:
        if not self.regime:
            return self.config.exposure_mult
        return exposure_mult_of(
            observed.spot_usd, observed.equity, observed.cushion_usd, self.config
        )

    def _wanted(self, observed: Snapshot) -> float:
        if not self.regime or self._regime is None:
            return self.config.exposure_mult
        return self._regime.target

    def _evaluate_regime(self, minute: Minute) -> None:
        """Une évaluation par jour à 00:00 UTC, comme le README §8.9 le demande.

        Une fois par jour et pas une fois par minute : le spread traverse ses
        seuils plusieurs fois dans une journée agitée, et une porte qui compte
        les minutes ferait de l'hystérésis de sept jours une hystérésis de sept
        minutes — soit aucune.
        """
        if not self.regime:
            return
        day = minute.ts_ms // DAY_MS
        if day == self._regime_day:
            return
        self._regime_day = day
        spread = self._funding.annualized(minute.ts_ms) - minute.borrow_apr
        if self._regime is None:
            # Au premier jour la porte tient ce que le montage tient déjà : elle
            # arbitre la suite, elle ne re-dimensionne pas au démarrage sur une
            # moyenne 30 jours qui n'a encore qu'un jour de données.
            self._regime = Regime(
                target=self.config.exposure_mult,
                candidate=regime_candidate(spread, self.config),
                days=1,
            )
            return
        before = self._regime.target
        self._regime = regime_step(self._regime, spread, self.config)
        if self._regime.target != before:
            self.journal.regime_changes.append((minute.ts_ms, self._regime.target))

    def _worth_stepping(self, action: Action, observed: Snapshot, minute: Minute) -> bool:
        """Les deux garde-fous que le README §8.9 demande et que le moteur du bot n'a pas.

        **Une tranche par heure au maximum**, écrit noir sur blanc au §8.9, et
        implémenté nulle part : `decide` est pur et n'a pas d'horloge pour ça.
        C'est la boucle qui la tient.

        **Une zone morte.** `_EXPOSURE_EPS` vaut 1e-6 dans `delta0/decision.py`,
        soit deux centimes de spot sur 20 000 $. Or un re-centrage ne pose jamais
        l'exposition au millionième : les coûts sont estimés en deux passes et le
        gaz sort d'une flotte hors bilan, ce qui laisse un résidu de l'ordre de
        1e-3. Mesuré : la porte a tiré 19 913 fois en quatre mois sur un écart de
        0,001 entre tenue (2,3518x) et voulue (2,3529x), et brûlé 20 384 $ de
        frais sur 20 000 $ de capital. Le seuil retenu ici est celui que le projet
        a déjà tranché pour « trop petit pour valoir une opération » :
        `skim_min_usd`, en dollars de spot déplacés.

        Les deux refus sont COMPTÉS. Un garde-fou qui travaille en silence est un
        garde-fou dont personne ne saura qu'il a tenu la campagne debout.
        """
        step = float(action.params["step_target_exposure_mult"])
        held = self._held(observed)
        moved = abs(step - held) * max(0.0, observed.equity - observed.cushion_usd)
        if moved < self.config.skim_min_usd:
            # La zone morte passe AVANT la cadence : un pas qui ne vaut rien n'a
            # pas à consommer le créneau horaire d'un pas qui vaudrait quelque chose.
            self.journal.regime_suppressed += 1
            return False
        if self._regime_step_ms is not None and minute.ts_ms - self._regime_step_ms < HOUR_MS:
            self.journal.regime_rate_limited += 1
            return False
        self._regime_step_ms = minute.ts_ms
        return True

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
