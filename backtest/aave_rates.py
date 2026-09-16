"""Le coût de l'emprunt Aave, relevé heure par heure dans les événements.

Aucune source gratuite ne sert l'historique complet du taux (DefiLlama ne donne
que le dépôt, l'API Aave remonte à un an, Aavescan est payant). Les événements
`ReserveDataUpdated`, eux, sont gratuits, exacts et au bloc.

**Ce sont les index qui comptent, pas l'APR.** Chaque événement publie
`variableBorrowIndex`, un cumul depuis l'origine de la réserve : l'intérêt
réellement couru entre deux instants est le rapport de deux index, un point
c'est tout. Rien à intégrer, aucune composition à supposer, aucun jour à
compter. Un backtest qui partirait d'un APR devrait choisir sa convention, et ce
choix pèserait sur le résultat sans jamais apparaître dans un chiffre.

**Un relevé par heure, pas tous les événements.** Mesuré le 2026-09-16 sur une
journée réelle d'USDC : 9 832 événements par jour, soit ~295 000 par mois et
~60 Mo de cache mensuel, pour 37 minutes de lecture. Or deux relevés suffisent à
donner l'intérêt exact entre eux, puisque le rapport de leurs index l'est. En
lisant une fenêtre étroite avant chaque repère horaire, le même mois coûte
~14 minutes et quelques dizaines de kilo-octets. La fenêtre part de 2 000 blocs
(62 événements sur l'USDC, le dernier à un bloc du repère) et s'élargit tant
qu'elle ne trouve rien : le wstETH, bien plus calme, demande 20 000 blocs.

Ce qui est gardé est toujours **un index publié**, jamais une interpolation. Un
relevé dit de combien de blocs il précède son repère (`stale_blocks`), et une
heure sans aucun événement est déclarée manquante plutôt que comblée.

Les autres garde-fous :

- **rien avant la date de listage d'une réserve.** L'USDC natif n'existe sur
  Aave v3 Arbitrum que depuis le 2023-06-28, le wstETH depuis le 2023-03-01 :
  demander avant, c'est rejouer un montage qui n'existait pas ;
- **un index cumulé ne décroît jamais.** S'il décroît, la lecture est corrompue
  et le portage paraîtrait gratuit : on lève ;
- **un index ne s'extrapole pas vers l'arrière.** Demander avant le premier
  relevé lève, avec la marche à suivre : charger le mois précédent ;
- **un mois n'est mis en cache que terminé.**
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from backtest.chain import Chain, topic_for_address, word

RAY = 10**27
HOUR_S = 3_600
MONTHS_IN_YEAR = 12

# Largeurs de fenêtre successives, en blocs, mesurées le 2026-09-16 : 2 000
# suffisent pour l'USDC, 20 000 pour le wstETH. Au-delà de la dernière, la
# réserve est dormante et l'heure est déclarée manquante.
WINDOWS = (2_000, 20_000, 200_000, 2_000_000)

# Topic de ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256),
# identique en v2 et v3, recalculé et confronté à un vrai log le 2026-09-16.
TOPIC_RESERVE_DATA_UPDATED = "0x804c9b842b2748a22bb64b345453a3de7ca54a6ca45ce00d415894979e22897a"

POOL_V3_ARBITRUM = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
POOL_V2_MAINNET = "0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9"

MonthProgress = Callable[[str, int, int], None]
Month = tuple[int, int]

_PARTIAL_SUFFIX = ".partial"


class AaveRatesError(Exception):
    """Ce qui a été lu ne peut pas servir de coût d'emprunt."""


@dataclass(frozen=True, slots=True)
class Reserve:
    """Une réserve, et la date à partir de laquelle elle a existé pour de bon."""

    name: str
    chain: str
    pool: str
    asset: str  # l'actif emprunté ou déposé, dans le vocabulaire d'Aave
    first_block: int  # premier ReserveDataUpdated vérifié
    listed_on: str  # sa date, en clair, pour que le refus soit lisible


RESERVES: dict[str, Reserve] = {
    "usdc-arbitrum": Reserve(
        name="usdc-arbitrum",
        chain="arbitrum",
        pool=POOL_V3_ARBITRUM,
        asset="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        first_block=105_770_341,
        listed_on="2023-06-28",
    ),
    "usdce-arbitrum": Reserve(
        name="usdce-arbitrum",
        chain="arbitrum",
        pool=POOL_V3_ARBITRUM,
        asset="0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
        first_block=8_001_136,
        listed_on="2022-03-16",
    ),
    "wsteth-arbitrum": Reserve(
        name="wsteth-arbitrum",
        chain="arbitrum",
        pool=POOL_V3_ARBITRUM,
        asset="0x5979D7b546E38E414F7E9822514be443A4800529",
        first_block=65_741_201,
        listed_on="2023-03-01",
    ),
    # Le proxy du segment PROXY, avant qu'Arbitrum ait un marché. L'adresse a été
    # confirmée par les faits le 2026-09-16 (117 événements sur 10 000 blocs de
    # mars 2022, emprunt variable 3,8644 %) ; une mauvaise adresse n'aurait rien
    # rendu. Le bloc ci-dessous est le plus ancien événement CONSTATÉ, pas la
    # date de lancement de la v2, qui reste NON VÉRIFIÉE et lui est antérieure.
    "usdc-v2-mainnet": Reserve(
        name="usdc-v2-mainnet",
        chain="mainnet",
        pool=POOL_V2_MAINNET,
        asset="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        first_block=11_400_000,
        listed_on="2020-12-06",
    ),
}


@dataclass(frozen=True, slots=True)
class RateEvent:
    """Un événement, tel qu'il est publié : cinq nombres en ray."""

    block: int
    liquidity_rate: int
    stable_borrow_rate: int
    variable_borrow_rate: int
    liquidity_index: int
    variable_borrow_index: int


@dataclass(frozen=True, slots=True)
class Mark:
    """Le dernier index publié avant un repère horaire, et son ancienneté."""

    ts: int  # le repère, UTC
    block: int  # le bloc de l'événement retenu
    liquidity_rate: int
    variable_borrow_rate: int
    liquidity_index: int
    variable_borrow_index: int
    stale_blocks: int  # de combien de blocs l'événement précède le repère

    @property
    def variable_borrow_apr(self) -> float:
        """L'APR affiché à cet instant. Informatif : le coût se lit sur les index."""
        return self.variable_borrow_rate / RAY


@dataclass(slots=True)
class Report:
    reserve: str
    fetched: list[Month] = field(default_factory=list)
    cached: list[Month] = field(default_factory=list)
    not_final: list[Month] = field(default_factory=list)
    missing_hours: list[int] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)

    @property
    def worst_stale_blocks(self) -> int:
        return max((mark.stale_blocks for mark in self.marks), default=0)


def month_bounds_s(year: int, month: int) -> tuple[int, int]:
    """Premier instant du mois, et premier instant du suivant, en secondes UTC."""
    start = calendar.timegm((year, month, 1, 0, 0, 0))
    days = calendar.monthrange(year, month)[1]
    return start, start + days * 24 * HOUR_S


def hours(year: int, month: int) -> list[int]:
    """Chaque repère horaire du mois."""
    start, end = month_bounds_s(year, month)
    return list(range(start, end, HOUR_S))


def months(start: Month, end: Month) -> list[Month]:
    year, month = start
    walked: list[Month] = []
    while (year, month) <= end:
        walked.append((year, month))
        year, month = (year + 1, 1) if month == MONTHS_IN_YEAR else (year, month + 1)
    return walked


def listing_month(reserve: Reserve) -> Month:
    listed = dt.date.fromisoformat(reserve.listed_on)
    return listed.year, listed.month


def month_file(root: Path, reserve: Reserve, year: int, month: int) -> Path:
    return root / "aave" / reserve.name / f"{year:04d}-{month:02d}.json"


def decode(entries: Sequence[Mapping[str, object]]) -> list[RateEvent]:
    """Les cinq mots d'un log, dans l'ordre lu sur un vrai événement."""
    events: list[RateEvent] = []
    for entry in entries:
        block, data = entry.get("blockNumber"), entry.get("data")
        if not isinstance(block, str) or not isinstance(data, str):
            raise AaveRatesError(f"log inexploitable : {str(entry)[:120]}")
        events.append(
            RateEvent(
                block=int(block, 16),
                liquidity_rate=word(data, 0),
                stable_borrow_rate=word(data, 1),
                variable_borrow_rate=word(data, 2),
                liquidity_index=word(data, 3),
                variable_borrow_index=word(data, 4),
            )
        )
    return events


def check(marks: Sequence[Mark], reserve: Reserve) -> None:
    """Un index cumulé ne décroît jamais, et les repères ne reculent pas."""
    previous: Mark | None = None
    for mark in marks:
        if previous is not None:
            if mark.ts <= previous.ts:
                raise AaveRatesError(f"{reserve.name} : repères non croissants à {mark.ts}")
            if mark.variable_borrow_index < previous.variable_borrow_index:
                raise AaveRatesError(
                    f"{reserve.name} : index d'emprunt en baisse au repère {mark.ts}"
                    " — lecture corrompue, le portage paraîtrait gratuit"
                )
        previous = mark


def sample_hour(chain: Chain, reserve: Reserve, ts: int, *, head: int) -> Mark | None:
    """Le dernier index publié avant ce repère, en élargissant la fenêtre au besoin.

    Rend None quand la réserve n'a rien émis même sur la plus large : l'heure est
    alors déclarée manquante, jamais comblée par une valeur inventée.
    """
    mark_block = min(chain.block_at(ts, head=head), head)
    if mark_block < reserve.first_block:
        return None
    for width in WINDOWS:
        start = max(mark_block - width + 1, reserve.first_block)
        raw = chain.logs_range(
            reserve.pool,
            [TOPIC_RESERVE_DATA_UPDATED, topic_for_address(reserve.asset)],
            start,
            mark_block,
            chunk=max(width, 1),
        )
        events = decode(raw)
        if events:
            last = events[-1]
            return Mark(
                ts=ts,
                block=last.block,
                liquidity_rate=last.liquidity_rate,
                variable_borrow_rate=last.variable_borrow_rate,
                liquidity_index=last.liquidity_index,
                variable_borrow_index=last.variable_borrow_index,
                stale_blocks=mark_block - last.block,
            )
        if start == reserve.first_block:
            break  # inutile d'élargir en deçà de l'existence de la réserve
    return None


def sample_month(
    chain: Chain, reserve: Reserve, year: int, month: int
) -> tuple[list[Mark], list[int]]:
    """Les relevés horaires du mois, et les heures restées sans événement."""
    head = chain.block_number()
    marks: list[Mark] = []
    missing: list[int] = []
    for ts in hours(year, month):
        mark = sample_hour(chain, reserve, ts, head=head)
        if mark is None:
            missing.append(ts)
        else:
            marks.append(mark)
    chain.flush()
    check(marks, reserve)
    return marks, missing


def store_month(
    root: Path,
    reserve: Reserve,
    year: int,
    month: int,
    marks: Sequence[Mark],
    missing: Sequence[int],
) -> Path:
    path = month_file(root, reserve, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "reserve": reserve.name,
        "month": f"{year:04d}-{month:02d}",
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "missing_hours": list(missing),
        "marks": [
            [
                mark.ts,
                mark.block,
                mark.liquidity_rate,
                mark.variable_borrow_rate,
                mark.liquidity_index,
                mark.variable_borrow_index,
                mark.stale_blocks,
            ]
            for mark in marks
        ],
    }
    partial = path.with_name(path.name + _PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document), encoding="ascii")
    os.replace(partial, path)
    return path


def read_month(root: Path, reserve: Reserve, year: int, month: int) -> tuple[list[Mark], list[int]]:
    path = month_file(root, reserve, year, month)
    if not path.is_file():
        raise AaveRatesError(f"{reserve.name} {year:04d}-{month:02d} absent du cache {root}")
    document = json.loads(path.read_text(encoding="ascii"))
    rows = document.get("marks")
    if not isinstance(rows, list):
        raise AaveRatesError(f"fichier de cache illisible : {path}")
    marks = [
        Mark(
            ts=int(row[0]),
            block=int(row[1]),
            liquidity_rate=int(row[2]),
            variable_borrow_rate=int(row[3]),
            liquidity_index=int(row[4]),
            variable_borrow_index=int(row[5]),
            stale_blocks=int(row[6]),
        )
        for row in rows
    ]
    check(marks, reserve)
    missing = document.get("missing_hours")
    return marks, [int(ts) for ts in missing] if isinstance(missing, list) else []


def is_cached(root: Path, reserve: Reserve, year: int, month: int) -> bool:
    return month_file(root, reserve, year, month).is_file()


def ensure_range(
    chain: Chain,
    root: Path,
    reserve: Reserve,
    start: Month,
    end: Month,
    *,
    now_ts: int | None = None,
    progress: MonthProgress | None = None,
) -> Report:
    """Les relevés horaires de la plage, lus une fois et gardés — sauf le mois en cours."""
    if start < listing_month(reserve):
        raise AaveRatesError(
            f"{reserve.name} n'est listée que depuis le {reserve.listed_on} :"
            f" {start[0]:04d}-{start[1]:02d} rejouerait un montage qui n'existait pas"
        )
    stamp = now_ts if now_ts is not None else int(dt.datetime.now(dt.UTC).timestamp())
    report = Report(reserve=reserve.name)
    for year, month in months(start, end):
        if is_cached(root, reserve, year, month):
            marks, missing = read_month(root, reserve, year, month)
            report.cached.append((year, month))
            state = "cached"
        else:
            marks, missing = sample_month(chain, reserve, year, month)
            if month_bounds_s(year, month)[1] <= stamp:
                store_month(root, reserve, year, month, marks, missing)
                report.fetched.append((year, month))
                state = "fetched"
            else:
                report.not_final.append((year, month))
                state = "partial"
        report.marks.extend(marks)
        report.missing_hours.extend(missing)
        if progress is not None:
            progress(state, year, month)
    check(report.marks, reserve)
    return report


def index_at(marks: Sequence[Mark], ts: int) -> int:
    """Le dernier index d'emprunt relevé à cet instant. Jamais extrapolé vers l'arrière."""
    seen: int | None = None
    for mark in marks:
        if mark.ts > ts:
            break
        seen = mark.variable_borrow_index
    if seen is None:
        raise AaveRatesError(
            f"aucun relevé à {ts} ou avant : l'index ne s'invente pas"
            " — charger le mois précédent pour avoir un point d'ancrage"
        )
    return seen


def borrow_growth(marks: Sequence[Mark], from_ts: int, to_ts: int) -> float:
    """Ce que la dette a grossi entre deux instants, en proportion.

    C'est le rapport de deux index cumulés : l'intérêt réellement couru, sans
    convention de composition à choisir.
    """
    if to_ts < from_ts:
        raise AaveRatesError(f"plage à l'envers : {from_ts} -> {to_ts}")
    return index_at(marks, to_ts) / index_at(marks, from_ts) - 1.0
