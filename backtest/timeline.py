"""La frise à la minute : six séries, trois rythmes, deux places, un seul fil.

Le moteur du backtest veut une chose simple — l'état du monde à la minute N —
et les sources ne la donnent jamais ensemble : les bougies sont à la minute, le
ratio Lido à la journée, l'index d'emprunt Aave au relevé, le funding au
versement. Recoudre tout ça demande des conventions, et une convention tue un
backtest quand elle est prise au fond d'une boucle sans être nommée. Elles sont
donc toutes ici, et il n'y en a pas ailleurs.

- **Une minute n'existe que si les deux bougies existent.** Le prix du
  collatéral vient du spot, le prix qui peut liquider le short vient du mark ;
  une minute qui n'a que l'un des deux n'est pas une minute calme, c'est une
  minute qu'on ne sait pas jouer. Elle est comptée et sautée, jamais comblée.
- **Rien ne se lit vers l'avant.** Le ratio Lido et l'index Aave sont relevés
  moins souvent que la minute : on prend le dernier relevé À OU AVANT elle.
  Prendre le suivant ferait décider le bot sur un rendement qui n'était pas
  encore acquis — l'erreur classique qui rend un backtest flatteur.
- **La dette grossit par un rapport d'index, jamais par un APR intégré.**
  Chaque minute porte le facteur de croissance depuis la précédente ; sur un
  relevé quotidien il vaut 1,0 la plupart du temps et saute une fois par jour.
  Le total sur la période reste exact, seule l'attribution intrajournalière est
  grossière (`docs/backtest/inventaire-taux-staking.md`).
- **La réserve d'emprunt change avec la date**, parce que le marché a changé :
  USDC natif sur Arbitrum depuis le 2023-06-28, USDC.e avant, Aave v2 mainnet
  avant lui. Les index de deux réserves ne se divisent pas entre eux : au
  raccord le facteur vaut 1,0 et le raccord est COMPTÉ, pour qu'un rapport
  puisse dire combien d'intérêt la jointure a laissé de côté. Le mesurer sur
  les chevauchements est un travail à part, pas un défaut silencieux.
- **La place du funding change avec la date, pas avec le segment.** On prend
  Hyperliquid dès son premier versement servi et Binance avant : le plus fidèle
  disponible à chaque instant. Les versements de la phase 8 h d'Hyperliquid
  sortent marqués NON VÉRIFIÉ — personne n'a tranché si leur taux vaut par 8 h
  ou par heure, et c'est un facteur 8 sur le coût du portage.

Ce module ne charge jamais cinq ans en mémoire : il avance mois par mois et ne
garde que ce qu'un raccord demande.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Generic, TypeVar

from backtest import aave_rates, hl_funding, lido
from backtest.binance import MINUTE_MS, SERIES, Candle, Funding, Series, month_bounds_ms, months
from backtest.cache import CacheMissError, Month
from backtest.cache import read_month as read_candles
from backtest.funding import read_funding_month

SPOT: Series = SERIES["spot"]
MARK: Series = SERIES["mark"]

# Les archives Binance vivent sous `<racine du cache>/binance`, les séries de
# chaîne dans leurs propres dossiers à côté — même disposition que la commande
# de collecte, qui est la seule à écrire ici.
ARCHIVES = "binance"

T = TypeVar("T")

# Les réserves de DETTE, dans l'ordre où elles se sont succédé. Le wstETH est
# une réserve aussi, mais c'est le collatéral : son index ne nous coûte rien.
# Les dates ne sont pas recopiées ici, elles viennent du `listed_on` de chaque
# réserve — une date écrite à deux endroits finit par diverger.
DEBT_RESERVES = ("usdc-arbitrum", "usdce-arbitrum", "usdc-v2-mainnet")


class TimelineError(Exception):
    """Le monde ne peut pas être reconstitué à cette minute-là."""


class Segment(StrEnum):
    """Les trois fidélités du README §15.3. Chaque minute porte la sienne.

    Un rapport qui totalise les trois sans les séparer additionne un montage
    réel et une simulation de règles sur des places qui n'existaient pas.
    """

    FIDELE = "FIDÈLE"
    INTERMEDIAIRE = "INTERMÉDIAIRE"
    PROXY = "PROXY"


def _ms(day: str) -> int:
    """Minuit UTC de ce jour, en millisecondes."""
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp() * 1000)


# README §15.3. Le montage exact — wstETH collatéral ET USDC natif — n'existe
# que depuis le second ; le wstETH seul était listé depuis le premier.
INTERMEDIAIRE_FROM_MS = _ms("2023-03-01")
FIDELE_FROM_MS = _ms("2023-06-28")


def segment_at(ts_ms: int) -> Segment:
    if ts_ms >= FIDELE_FROM_MS:
        return Segment.FIDELE
    if ts_ms >= INTERMEDIAIRE_FROM_MS:
        return Segment.INTERMEDIAIRE
    return Segment.PROXY


# Construit une fois : `reserve_at` est appelé des millions de fois sur cinq ans,
# et retrier trois réserves à chaque minute se paie en minutes de calcul.
_DEBT_TIMELINE: tuple[tuple[int, aave_rates.Reserve], ...] = tuple(
    sorted(
        (
            (_ms(aave_rates.RESERVES[name].listed_on), aave_rates.RESERVES[name])
            for name in DEBT_RESERVES
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
)


def debt_reserves() -> tuple[tuple[int, aave_rates.Reserve], ...]:
    """Les réserves de dette, de la plus récente à la plus ancienne, datées."""
    return _DEBT_TIMELINE


def reserve_at(ts_ms: int) -> aave_rates.Reserve:
    """La réserve où la dette vivait à cet instant."""
    for since, reserve in _DEBT_TIMELINE:
        if ts_ms >= since:
            return reserve
    raise _too_early()


def spans(first: int, stop: int) -> list[tuple[int, int, aave_rates.Reserve]]:
    """La plage découpée là où la dette a changé de marché, dans l'ordre du temps.

    Découper plutôt que résoudre la réserve minute par minute garde un curseur
    d'index par marché : deux index cumulés ne partagent pas de curseur, et un
    curseur qui saute de l'un à l'autre ne sait plus dire s'il recule.
    """
    cut: list[tuple[int, int, aave_rates.Reserve]] = []
    edge = stop
    for since, reserve in _DEBT_TIMELINE:
        if since >= stop:
            continue
        begins = max(since, first)
        if begins < edge:
            cut.append((begins, edge, reserve))
        edge = begins
        if since <= first:
            break
    if edge > first:
        raise _too_early()
    return sorted(cut)


def _too_early() -> TimelineError:
    return TimelineError(
        f"aucun marché d'emprunt avant le {_DEBT_TIMELINE[-1][1].listed_on} :"
        " la frise ne remonte pas plus haut"
    )


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """Un versement dû, tel que sa place l'a servi.

    `rate` couvre `interval_hours`, jamais une heure par défaut : les deux
    places ont changé de rythme, et sommer des taux de périodes différentes est
    la façon la plus discrète de se tromper d'un facteur 8.
    """

    ts_ms: int
    rate: float
    interval_hours: int
    venue: str
    unverified: bool = False


@dataclass(frozen=True, slots=True)
class Minute:
    """L'état du monde à une minute, tout ce que le moteur a besoin de savoir."""

    ts_ms: int
    eth: Candle  # spot Binance — le proxy de l'ETH/USD que l'oracle Aave applique
    mark: Candle  # mark futures Binance — le proxy du mark Hyperliquid
    ratio: float  # ETH par wstETH (`stEthPerToken`), relevé du jour
    borrow_index: int  # `variableBorrowIndex` en ray, dernier relevé
    borrow_factor: float  # croissance de la dette depuis la minute précédente
    reserve: str  # la réserve d'où vient cet index
    segment: Segment
    funding: FundingEvent | None = None

    @property
    def ts(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.ts_ms / 1000, dt.UTC)


@dataclass(slots=True)
class Gaps:
    """Ce que la frise n'a pas pu jouer, et ce qu'elle a dû raccorder.

    Décrit, ne corrige pas. Un backtest qui saute 3 000 minutes sans le dire
    rend un résultat sur une période qui n'est pas celle qu'on croit.
    """

    minutes: int = 0
    spot_only: int = 0
    mark_only: int = 0
    absent: int = 0
    reserve_splices: list[int] = field(default_factory=list)
    unverified_funding: int = 0

    @property
    def skipped(self) -> int:
        return self.spot_only + self.mark_only + self.absent

    def describe(self) -> str:
        parts = [f"{self.minutes} minutes jouées"]
        if self.skipped:
            parts.append(
                f"{self.skipped} sautées ({self.spot_only} sans mark, "
                f"{self.mark_only} sans spot, {self.absent} sans rien)"
            )
        if self.reserve_splices:
            parts.append(f"{len(self.reserve_splices)} raccord(s) de réserve")
        if self.unverified_funding:
            parts.append(f"{self.unverified_funding} versement(s) NON VÉRIFIÉ(S)")
        return ", ".join(parts)


class Cursor(Generic[T]):
    """Le dernier relevé à ou avant une minute, sur une série plus lente qu'elle.

    Relire la série depuis son début à chaque minute coûterait des milliards de
    comparaisons sur cinq ans ; le curseur n'avance que vers l'avant, ce que la
    frise garantit. Il REFUSE de reculer plutôt que de rendre une valeur fausse :
    un curseur qu'on fait revenir en arrière rendrait la valeur d'après, et une
    valeur d'après est exactement ce qu'un backtest ne doit jamais voir.
    """

    def __init__(self, series: Sequence[tuple[int, T]], what: str) -> None:
        self._series = series
        self._what = what
        self._at = -1

    def at(self, ts_ms: int) -> T:
        while self._at + 1 < len(self._series) and self._series[self._at + 1][0] <= ts_ms:
            self._at += 1
        if self._at < 0:
            raise TimelineError(
                f"aucun relevé {self._what} à {ts_ms} ou avant — une valeur ne s'invente pas"
            )
        if self._series[self._at][0] > ts_ms:
            raise TimelineError(f"curseur {self._what} ramené en arrière jusqu'à {ts_ms}")
        return self._series[self._at][1]


def lido_series(points: Sequence[lido.Point]) -> list[tuple[int, float]]:
    """Les relevés Lido datés en millisecondes, prêts pour un curseur.

    Jamais interpolé vers l'avant : le rendement d'une journée n'est acquis
    qu'à la fin de la journée. L'écart que cela coûte est d'un jour de staking,
    soit 0,0074 % du collatéral — sans effet sur un seuil, contrairement à un
    ratio lu en avance, qui rend le facteur de santé meilleur qu'il n'était.
    """
    return [(lido.day_start_ts(point.day) * 1000, point.ratio) for point in points]


def funding_schedule(
    root: Path, year: int, month: int, *, hl_available: bool = True
) -> list[FundingEvent]:
    """Les versements d'un mois, pris à la place la plus fidèle qui cotait.

    Hyperliquid dès son premier versement servi, Binance avant lui. Le mois de
    la bascule se coupe à l'instant exact du premier versement HL, pas à une
    frontière de mois : une demi-journée de funding comptée deux fois serait
    invisible dans un total annuel.
    """
    binance: list[Funding] = []
    try:
        binance = read_funding_month(root / ARCHIVES, year, month)
    except CacheMissError:
        binance = []

    # Un mois HL absent du cache n'est pas une anomalie : la place ne cotait pas
    # l'ETH avant 2023-05. Un mois PRÉSENT mais illisible en est une, et il
    # remonte : retomber sur Binance en silence changerait la place du funding
    # au milieu de la période, ce qu'aucun rapport ne montrerait.
    hl_rows: list[hl_funding.HlFunding] = []
    if hl_available and (year, month) >= hl_funding.FIRST_MONTH:
        try:
            hl_rows = hl_funding.read_month(root, year, month)
        except CacheMissError:
            hl_rows = []

    events: list[FundingEvent] = []
    switch_ms = hl_rows[0].ts_ms if hl_rows else None
    for row in binance:
        if switch_ms is not None and row.ts_ms >= switch_ms:
            continue
        events.append(
            FundingEvent(
                ts_ms=row.ts_ms,
                rate=row.rate,
                interval_hours=row.interval_hours,
                venue="binance",
            )
        )
    for common in hl_funding.as_funding(hl_rows):
        events.append(
            FundingEvent(
                ts_ms=common.ts_ms,
                rate=common.rate,
                interval_hours=common.interval_hours,
                venue="hyperliquid",
                # La phase 8 h : l'API ne dit pas si le taux vaut par 8 h ou par
                # heure. Marqué plutôt que tranché, pour qu'un rapport puisse
                # chiffrer ce qui repose dessus.
                unverified=common.interval_hours == hl_funding.EIGHT_HOURS,
            )
        )
    events.sort(key=lambda event: event.ts_ms)
    return events


@dataclass
class Timeline:
    """La frise, avancée mois par mois, et le compte de ce qu'elle a sauté."""

    root: Path
    step_s: int = aave_rates.DAY_S
    gaps: Gaps = field(default_factory=Gaps)
    _index: int | None = None
    _reserve: str | None = None

    def walk(self, start: Month, end: Month) -> Iterator[Minute]:
        """Chaque minute jouable de la plage, dans l'ordre."""
        points = sorted(lido.load(self.root).values(), key=lambda point: point.day)
        if not points:
            raise TimelineError(
                f"cache Lido vide sous {self.root} — "
                "lancer `python -m backtest.download --series lido`"
            )
        self._index = None
        self._reserve = None
        for year, month in months(start, end):
            yield from self._month(year, month, points)

    def _factor(self, index: int, reserve: str, ts_ms: int) -> float:
        """La croissance de la dette depuis la minute précédente.

        Au changement de réserve le rapport n'a pas de sens : deux marchés
        cumulent chacun leur propre index depuis leur propre origine. On pose
        1,0 et on compte le raccord, plutôt que de diviser deux nombres qui ne
        parlent pas de la même dette.
        """
        factor = 1.0
        if self._reserve != reserve:
            if self._reserve is not None:
                self.gaps.reserve_splices.append(ts_ms)
        elif self._index is not None:
            factor = index / self._index
        self._index = index
        self._reserve = reserve
        return factor

    def _month(self, year: int, month: int, points: Sequence[lido.Point]) -> Iterator[Minute]:
        """Un mois : bougies jointes, séries lentes résolues, versements posés."""
        archives = self.root / ARCHIVES
        try:
            spot = {candle.ts_ms: candle for candle in read_candles(archives, SPOT, year, month)}
            mark = {candle.ts_ms: candle for candle in read_candles(archives, MARK, year, month)}
        except CacheMissError as absent:
            raise TimelineError(
                f"{absent} — un mois absent au milieu d'une plage se lirait comme "
                "un mois calme ; lancer `python -m backtest.download`"
            ) from absent
        due = {
            event.ts_ms // MINUTE_MS: event for event in funding_schedule(self.root, year, month)
        }

        first, stop = month_bounds_ms(year, month)
        ratios: Cursor[float] = Cursor(lido_series(points), "Lido")

        # Un mois peut être à cheval sur deux marchés : l'USDC natif arrive le
        # 2023-06-28, en plein mois de juin. Prendre la réserve du 1er du mois
        # ferait lire trois jours du segment FIDÈLE sur le marché de l'USDC.e,
        # juste là où le montage devient exact.
        for begins, ends, reserve in spans(first, stop):
            indices: Cursor[int] = Cursor(
                [
                    (reading.ts * 1000, reading.variable_borrow_index)
                    for reading in self._marks(reserve, year, month)
                ],
                f"Aave {reserve.name}",
            )
            for ts_ms in range(begins, ends, MINUTE_MS):
                here = spot.get(ts_ms)
                there = mark.get(ts_ms)
                if here is None or there is None:
                    if there is not None:
                        self.gaps.mark_only += 1
                    elif here is not None:
                        self.gaps.spot_only += 1
                    else:
                        self.gaps.absent += 1
                    continue
                index = indices.at(ts_ms)
                event = due.get(ts_ms // MINUTE_MS)
                self.gaps.minutes += 1
                if event is not None and event.unverified:
                    self.gaps.unverified_funding += 1
                yield Minute(
                    ts_ms=ts_ms,
                    eth=here,
                    mark=there,
                    ratio=ratios.at(ts_ms),
                    borrow_index=index,
                    borrow_factor=self._factor(index, reserve.name, ts_ms),
                    reserve=reserve.name,
                    segment=segment_at(ts_ms),
                    funding=event,
                )

    def _marks(self, reserve: aave_rates.Reserve, year: int, month: int) -> list[aave_rates.Mark]:
        """Les relevés du mois, précédés de ceux d'avant : il faut un point d'ancrage.

        Le premier relevé d'un mois tombe à 00:00 le 1er, donc il ancre la
        première minute — sauf s'il manque, et un mois qui commence par un trou
        est exactement le cas où l'on veut le mois d'avant plutôt qu'un refus.
        """
        before = (year - 1, 12) if month == 1 else (year, month - 1)
        marks: list[aave_rates.Mark] = []
        for taken in (before, (year, month)):
            try:
                found, _ = aave_rates.read_month(self.root, reserve, *taken, step_s=self.step_s)
            except aave_rates.AaveRatesError:
                continue
            marks.extend(found)
        if not marks:
            raise TimelineError(
                f"aucun relevé {reserve.name} pour {year:04d}-{month:02d} — lancer "
                f"`python -m backtest.download --series aave --aave-reserve {reserve.name}`"
            )
        marks.sort(key=lambda mark: mark.ts)
        return marks
