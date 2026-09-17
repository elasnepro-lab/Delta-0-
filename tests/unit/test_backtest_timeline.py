"""Chantier 7.3 — la frise à la minute, et les conventions de jointure.

Ce qui est défendu ici n'est pas la lecture des sources : chaque source a déjà
ses tests. C'est ce qui se passe quand on les recoud, et qui ne se voit pas dans
un total annuel :

- une valeur lue **en avance** (le ratio de demain appliqué à aujourd'hui) rend
  un backtest flatteur sans jamais rien casser ;
- une minute **sautée en silence** raccourcit la période sans le dire ;
- un index d'emprunt **divisé à travers un changement de réserve** fabrique un
  intérêt qui n'a jamais couru ;
- un versement de funding **compté deux fois** au mois de la bascule Binance →
  Hyperliquid disparaît dans la moyenne.

Le cache est écrit par les écrivains du collecteur, pas par des fichiers posés à
la main : un test qui fabrique son propre format ne teste plus le même cache.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from backtest import aave_rates, hl_funding, lido
from backtest.binance import MINUTE_MS, SERIES, month_bounds_ms
from backtest.cache import store
from backtest.timeline import (
    ARCHIVES,
    Cursor,
    Minute,
    Segment,
    Timeline,
    TimelineError,
    funding_schedule,
    lido_series,
    reserve_at,
    segment_at,
)
from tests.binance_archives import build_csv, build_zip, funding_archive

USDC = aave_rates.RESERVES["usdc-arbitrum"]
USDCE = aave_rates.RESERVES["usdce-arbitrum"]

JULY_2023 = (2023, 7)  # segment FIDÈLE, USDC natif, funding HL horaire
DAY_MS = 86_400_000


def ms(day: str) -> int:
    return lido.day_start_ts(day) * 1000


# --- les règles datées --------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2021-05-19", Segment.PROXY),
        ("2023-02-28", Segment.PROXY),
        ("2023-03-01", Segment.INTERMEDIAIRE),  # borne incluse : le wstETH est listé ce jour
        ("2023-06-27", Segment.INTERMEDIAIRE),
        ("2023-06-28", Segment.FIDELE),  # l'USDC natif arrive, le montage exact existe
        ("2026-09-17", Segment.FIDELE),
    ],
)
def test_segment_borne_incluse(day: str, expected: Segment) -> None:
    assert segment_at(ms(day)) is expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-01-01", "usdc-arbitrum"),
        ("2023-06-28", "usdc-arbitrum"),
        ("2023-06-27", "usdce-arbitrum"),  # la veille, la dette vivait encore sur l'USDC.e
        ("2022-06-01", "usdce-arbitrum"),
        ("2021-05-19", "usdc-v2-mainnet"),
    ],
)
def test_reserve_suit_la_date(day: str, expected: str) -> None:
    assert reserve_at(ms(day)).name == expected


def test_reserve_refuse_avant_le_premier_marche() -> None:
    with pytest.raises(TimelineError, match="aucun marché d'emprunt"):
        reserve_at(ms("2019-01-01"))


# --- le curseur ---------------------------------------------------------------


def test_curseur_rend_le_dernier_releve_jamais_le_suivant() -> None:
    cursor: Cursor[str] = Cursor([(100, "a"), (200, "b"), (300, "c")], "essai")
    assert cursor.at(150) == "a"  # 200 existe, mais il est dans le futur de 150
    assert cursor.at(200) == "b"
    assert cursor.at(999) == "c"


def test_curseur_refuse_avant_le_premier_releve() -> None:
    cursor: Cursor[str] = Cursor([(100, "a")], "essai")
    with pytest.raises(TimelineError, match="ne s'invente pas"):
        cursor.at(50)


def test_curseur_refuse_de_reculer() -> None:
    """Reculer rendrait la valeur d'après — exactement ce qu'un backtest ne doit pas voir."""
    cursor: Cursor[str] = Cursor([(100, "a"), (200, "b")], "essai")
    assert cursor.at(250) == "b"
    with pytest.raises(TimelineError, match="ramené en arrière"):
        cursor.at(150)


def test_le_ratio_lido_est_celui_du_jour_pas_du_lendemain() -> None:
    points = [
        lido.Point(day="2023-07-01", block=1, raw=1_100_000_000_000_000_000),
        lido.Point(day="2023-07-02", block=2, raw=1_200_000_000_000_000_000),
    ]
    cursor: Cursor[float] = Cursor(lido_series(points), "Lido")
    assert cursor.at(ms("2023-07-01") + 23 * 3_600_000) == pytest.approx(1.1)
    assert cursor.at(ms("2023-07-02")) == pytest.approx(1.2)


# --- le cache, écrit par les écrivains du collecteur ---------------------------


def write_candles(root: Path, series_name: str, year: int, month: int, count: int) -> None:
    start = month_bounds_ms(year, month)[0]
    payload = build_zip(build_csv(count, start=start))
    store(
        root / ARCHIVES,
        SERIES[series_name],
        year,
        month,
        payload,
        hashlib.sha256(payload).hexdigest(),
    )


def write_binance_funding(root: Path, year: int, month: int, *, rows: int = 3) -> None:
    payload = funding_archive(year, month, rows=rows)
    store(
        root / ARCHIVES,
        SERIES["funding"],
        year,
        month,
        payload,
        hashlib.sha256(payload).hexdigest(),
    )


def write_lido(root: Path, first_day: str, days: int = 3) -> None:
    points = {}
    for index in range(days):
        day = dt.date.fromisoformat(first_day) + dt.timedelta(days=index)
        points[day.isoformat()] = lido.Point(
            day=day.isoformat(),
            block=1_000 + index,
            raw=1_200_000_000_000_000_000 + index * 10**14,
        )
    lido.save(root, points)


def write_aave(
    root: Path,
    reserve: aave_rates.Reserve,
    year: int,
    month: int,
    *,
    index: int = aave_rates.RAY,
    step_index: int = 10**20,
) -> None:
    """Un relevé par jour, index cumulé strictement croissant."""
    start, end = aave_rates.month_bounds_s(year, month)
    marks = [
        aave_rates.Mark(
            ts=ts,
            block=1_000 + number,
            liquidity_rate=10**25,
            variable_borrow_rate=5 * 10**25,
            liquidity_index=aave_rates.RAY,
            variable_borrow_index=index + number * step_index,
            stale_blocks=3,
        )
        for number, ts in enumerate(range(start, end, aave_rates.DAY_S))
    ]
    aave_rates.store_month(root, reserve, year, month, marks, [], step_s=aave_rates.DAY_S)


def build_cache(root: Path, *, candles: int = 5, hl_rows: int = 0) -> None:
    year, month = JULY_2023
    write_candles(root, "spot", year, month, candles)
    write_candles(root, "mark", year, month, candles)
    write_binance_funding(root, year, month)
    write_lido(root, "2023-07-01")
    write_aave(root, USDC, year, month)
    if hl_rows:
        start = month_bounds_ms(year, month)[0]
        hl_funding.store_month(
            root,
            year,
            month,
            [
                {
                    "coin": "ETH",
                    "time": start + index * 3_600_000,
                    "fundingRate": "0.0000125",
                    "premium": "0.0",
                }
                for index in range(hl_rows)
            ],
        )


# --- la frise -----------------------------------------------------------------


def walked(root: Path) -> tuple[list[Minute], Timeline]:
    timeline = Timeline(root=root)
    return list(timeline.walk(JULY_2023, JULY_2023)), timeline


def test_une_minute_porte_tout_ce_que_le_moteur_demande(tmp_path: Path) -> None:
    build_cache(tmp_path)
    minutes, timeline = walked(tmp_path)

    assert len(minutes) == 5
    first = minutes[0]
    assert first.ts_ms == month_bounds_ms(*JULY_2023)[0]
    assert first.segment is Segment.FIDELE
    assert first.reserve == "usdc-arbitrum"
    assert first.ratio == pytest.approx(1.2)
    assert first.borrow_index == aave_rates.RAY
    assert first.eth.close > 0
    assert first.mark.close > 0
    assert timeline.gaps.minutes == 5


def test_une_minute_sans_mark_est_comptee_pas_comblee(tmp_path: Path) -> None:
    """Le prix du collatéral seul ne fait pas une minute : le short a son propre prix."""
    build_cache(tmp_path)
    write_candles(tmp_path, "mark", *JULY_2023, 2)  # deux minutes de mark contre cinq de spot

    minutes, timeline = walked(tmp_path)
    assert len(minutes) == 2
    assert timeline.gaps.spot_only == 3
    assert timeline.gaps.mark_only == 0
    assert "3 sans mark" in timeline.gaps.describe()


def test_les_minutes_absentes_des_deux_series_sont_comptees(tmp_path: Path) -> None:
    build_cache(tmp_path, candles=5)
    _, timeline = walked(tmp_path)
    total = (month_bounds_ms(*JULY_2023)[1] - month_bounds_ms(*JULY_2023)[0]) // MINUTE_MS
    assert timeline.gaps.absent == total - 5


def test_le_facteur_de_dette_est_un_rapport_dindex(tmp_path: Path) -> None:
    """La dette ne grossit que par le rapport de deux index cumulés — jamais par un APR."""
    build_cache(tmp_path)
    minutes, _ = walked(tmp_path)

    assert minutes[0].borrow_factor == 1.0  # rien avant la première minute
    assert all(minute.borrow_factor == 1.0 for minute in minutes[1:])

    # Le saut tombe au relevé suivant, pas à la minute suivante : cinq bougies
    # tiennent dans la première journée, donc l'index n'a pas encore bougé.
    assert {minute.borrow_index for minute in minutes} == {aave_rates.RAY}


def test_le_facteur_saute_au_releve_suivant(tmp_path: Path) -> None:
    build_cache(tmp_path, candles=0)
    year, month = JULY_2023
    start = month_bounds_ms(year, month)[0]
    # Deux bougies encadrant le relevé de J+1 : la veille à 23:59, et minuit pile.
    payload_start = start + DAY_MS - MINUTE_MS
    for name in ("spot", "mark"):
        raw = build_zip(build_csv(2, start=payload_start))
        store(
            tmp_path / ARCHIVES,
            SERIES[name],
            year,
            month,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

    minutes, _ = walked(tmp_path)
    assert len(minutes) == 2
    assert minutes[0].borrow_factor == 1.0
    assert minutes[1].borrow_factor == pytest.approx((aave_rates.RAY + 10**20) / aave_rates.RAY)


def test_un_mois_absent_refuse_et_nomme_la_commande(tmp_path: Path) -> None:
    build_cache(tmp_path)
    timeline = Timeline(root=tmp_path)
    with pytest.raises(TimelineError, match=r"backtest\.download"):
        list(timeline.walk((2023, 8), (2023, 8)))


def test_cache_lido_vide_refuse(tmp_path: Path) -> None:
    timeline = Timeline(root=tmp_path)
    with pytest.raises(TimelineError, match="cache Lido vide"):
        list(timeline.walk(JULY_2023, JULY_2023))


# --- le funding ---------------------------------------------------------------


def test_hyperliquid_prend_la_main_a_son_premier_versement(tmp_path: Path) -> None:
    """Au mois de la bascule, aucune heure n'est comptée deux fois.

    Les lignes Binance postérieures au premier versement HL sont écartées : les
    deux places auraient chargé le même portage, et un doublement de coût passe
    inaperçu dans une moyenne annuelle.
    """
    year, month = JULY_2023
    write_binance_funding(tmp_path, year, month, rows=6)  # toutes les 8 h
    start = month_bounds_ms(year, month)[0]
    switch = start + 16 * 3_600_000
    hl_funding.store_month(
        tmp_path,
        year,
        month,
        [
            {
                "coin": "ETH",
                "time": switch + index * 3_600_000,
                "fundingRate": "0.0000125",
                "premium": "0.0",
            }
            for index in range(3)
        ],
    )

    events = funding_schedule(tmp_path, year, month)
    assert [event.venue for event in events] == ["binance"] * 2 + ["hyperliquid"] * 3
    assert all(event.ts_ms < switch for event in events if event.venue == "binance")
    assert [event.ts_ms for event in events] == sorted(event.ts_ms for event in events)


def test_la_periode_est_portee_par_le_versement(tmp_path: Path) -> None:
    """Un taux 8 h et un taux horaire ne se somment pas pareil ; la période voyage avec."""
    year, month = JULY_2023
    write_binance_funding(tmp_path, year, month, rows=2)
    events = funding_schedule(tmp_path, year, month)
    assert {event.interval_hours for event in events} == {8}


def test_la_phase_8h_dhyperliquid_sort_marquee_non_verifiee(tmp_path: Path) -> None:
    """Facteur 8 sur le coût du portage : marqué, pas tranché au fond d'une boucle."""
    year, month = (2023, 5)
    start = month_bounds_ms(year, month)[0]
    hl_funding.store_month(
        tmp_path,
        year,
        month,
        [
            {
                "coin": "ETH",
                "time": start + 12 * 3_600_000,
                "fundingRate": "0.0001",
                "premium": "0.0",
            }
        ],
    )
    events = funding_schedule(tmp_path, year, month)
    assert [(event.interval_hours, event.unverified) for event in events] == [(8, True)]


def test_apres_la_bascule_horaire_le_versement_est_verifie(tmp_path: Path) -> None:
    year, month = JULY_2023
    start = month_bounds_ms(year, month)[0]
    hl_funding.store_month(
        tmp_path,
        year,
        month,
        [{"coin": "ETH", "time": start + 3_600_000, "fundingRate": "0.0000125", "premium": "0.0"}],
    )
    events = funding_schedule(tmp_path, year, month)
    assert [(event.interval_hours, event.unverified) for event in events] == [(1, False)]


def test_le_versement_se_pose_sur_sa_minute_et_sur_une_seule(tmp_path: Path) -> None:
    """Un versement est un événement daté, pas un coût étalé : il a une minute, une seule."""
    build_cache(tmp_path, candles=3, hl_rows=2)
    minutes, timeline = walked(tmp_path)

    porteuses = [minute for minute in minutes if minute.funding is not None]
    assert [minute.ts_ms for minute in porteuses] == [month_bounds_ms(*JULY_2023)[0]]
    assert porteuses[0].funding is not None
    assert porteuses[0].funding.venue == "hyperliquid"
    assert porteuses[0].funding.interval_hours == 1
    # Le second versement tombe à 01:00, hors des trois minutes jouées.
    assert timeline.gaps.unverified_funding == 0


# --- le raccord de réserve ----------------------------------------------------


JUNE_2023 = (2023, 6)  # le mois à cheval : USDC.e jusqu'au 27, USDC natif à partir du 28


def write_june_across_the_switch(root: Path) -> None:
    """Deux minutes de part et d'autre du 2023-06-28, et un marché de chaque côté."""
    year, month = JUNE_2023
    start = month_bounds_ms(year, month)[0]
    eve = ms("2023-06-27") + 23 * 3_600_000 + 59 * MINUTE_MS  # 27 juin 23:59
    for name in ("spot", "mark"):
        payload = build_zip(build_csv(2, start=eve))
        store(
            root / ARCHIVES,
            SERIES[name],
            year,
            month,
            payload,
            hashlib.sha256(payload).hexdigest(),
        )
    write_binance_funding(root, year, month)
    write_lido(root, "2023-06-01", days=30)
    write_aave(root, USDCE, year, month, index=2 * aave_rates.RAY)
    write_aave(root, USDC, year, month, index=aave_rates.RAY)
    assert start < eve  # le mois commence bien avant les deux minutes jouées


def test_le_raccord_de_reserve_ne_divise_pas_deux_index_etrangers(tmp_path: Path) -> None:
    """Deux marchés cumulent chacun leur index depuis leur propre origine.

    Les diviser fabriquerait ici une dette divisée par deux d'une minute à
    l'autre — un cadeau invisible dans un total annuel. Le facteur vaut 1,0 et
    le raccord est compté, pour qu'un rapport puisse dire ce qu'il a laissé.
    """
    write_june_across_the_switch(tmp_path)
    timeline = Timeline(root=tmp_path)
    minutes = list(timeline.walk(JUNE_2023, JUNE_2023))

    assert [minute.reserve for minute in minutes] == ["usdce-arbitrum", "usdc-arbitrum"]
    assert [minute.segment for minute in minutes] == [Segment.INTERMEDIAIRE, Segment.FIDELE]
    assert minutes[1].borrow_factor == 1.0
    assert timeline.gaps.reserve_splices == [minutes[1].ts_ms]
    assert "1 raccord(s) de réserve" in timeline.gaps.describe()
