"""Chantier 7.2 — le funding Hyperliquid, paginé et mis en cache par mois.

Aucun test ne touche au réseau : une fausse API sert des pages de 500 lignes
comme la vraie, et compte les appels. Ce qui est épinglé tient en trois points
qui coûteraient cher plus tard — la pagination qui doit s'arrêter, le mois en
cours qu'on ne fige pas, et la période d'un versement qui vient d'une règle
datée et jamais de l'écart au voisin, sinon un trou se lirait comme un rythme.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from backtest.binance import month_bounds_ms
from backtest.cache import CacheMissError
from backtest.funding import gaps, intervals
from backtest.hl_funding import (
    COIN,
    EIGHT_HOURS,
    FIRST_MONTH,
    HOURLY_SINCE_MS,
    HlError,
    HlFunding,
    as_funding,
    declared_period_hours,
    fetch_month_rows,
    fill_range,
    is_cached,
    month_file,
    parse_rows,
    read_month,
    store_month,
)

HOUR_MS = 3_600_000


def ms(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC).timestamp() * 1000)


def row(ts_ms: int, rate: float = 0.00001) -> dict[str, Any]:
    """Une ligne au format exact de l'API, chaînes comprises."""
    return {"coin": COIN, "fundingRate": f"{rate:.10f}", "premium": "0.0003", "time": ts_ms}


def hourly(start_ms: int, count: int, *, step_ms: int = HOUR_MS) -> list[dict[str, Any]]:
    return [row(start_ms + index * step_ms + 42) for index in range(count)]


class FakeHyperliquid:
    """L'API /info, en mémoire : elle pagine par 500 et compte ses appels."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        start, end = body["startTime"], body["endTime"]
        window = [r for r in self.rows if start <= int(r["time"]) < end]
        return httpx.Response(200, json=window[:500])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


# --- Ce que l'API rend, et ce qu'elle refuse ----------------------------------


def test_a_refusal_served_with_a_200_is_not_taken_for_data() -> None:
    """Le même piège que le SDK côté bot : l'erreur arrive dans le corps, pas dans le code."""
    with pytest.raises(HlError, match="inattendue"):
        parse_rows({"error": "Something went wrong"})


def test_a_row_of_another_asset_is_refused() -> None:
    with pytest.raises(HlError, match="autre actif"):
        parse_rows([{"coin": "BTC", "fundingRate": "0.1", "premium": "0.1", "time": 1}])


# --- La pagination ------------------------------------------------------------


def test_a_month_is_paginated_until_the_api_runs_short(tmp_path: Path) -> None:
    """Une page pleine veut dire « il y en a peut-être d'autres » ; une page courte clôt le mois."""
    start = month_bounds_ms(2025, 10)[0]
    api = FakeHyperliquid(hourly(start, 744))  # un octobre horaire complet

    with api.client() as client:
        rows = fetch_month_rows(client, 2025, 10)

    assert len(rows) == 744
    assert len(api.calls) == 2, "500 puis 244 : deux pages, et on s'arrête"
    assert [int(r["time"]) for r in rows] == sorted(int(r["time"]) for r in rows)


def test_an_empty_month_asks_once_and_stops(tmp_path: Path) -> None:
    api = FakeHyperliquid([])
    with api.client() as client:
        assert fetch_month_rows(client, 2021, 3) == []
    assert len(api.calls) == 1


# --- Le cache -----------------------------------------------------------------


def test_a_finished_month_is_cached_and_reads_back(tmp_path: Path) -> None:
    start = month_bounds_ms(2025, 10)[0]
    api = FakeHyperliquid(hourly(start, 744))

    with api.client() as client:
        report = fill_range(client, tmp_path, (2025, 10), (2025, 10), now_ms=ms("2026-01-01T00:00"))

    assert report.fetched == [(2025, 10)]
    assert len(report.rows) == 744
    assert is_cached(tmp_path, 2025, 10)
    assert len(read_month(tmp_path, 2025, 10)) == 744


def test_the_rows_are_kept_exactly_as_the_api_served_them(tmp_path: Path) -> None:
    """Un mois du cache doit pouvoir se confronter à la source des années plus tard."""
    start = month_bounds_ms(2025, 10)[0]
    served = hourly(start, 3)
    store_month(tmp_path, 2025, 10, served)

    document = json.loads(month_file(tmp_path, 2025, 10).read_text(encoding="ascii"))
    assert document["rows"] == served
    assert document["month"] == "2025-10"


def test_a_cached_month_costs_no_request(tmp_path: Path) -> None:
    start = month_bounds_ms(2025, 10)[0]
    api = FakeHyperliquid(hourly(start, 10))
    with api.client() as client:
        fill_range(client, tmp_path, (2025, 10), (2025, 10), now_ms=ms("2026-01-01T00:00"))
        after_first = len(api.calls)
        again = fill_range(client, tmp_path, (2025, 10), (2025, 10), now_ms=ms("2026-01-01T00:00"))

    assert again.cached == [(2025, 10)]
    assert len(again.rows) == 10
    assert len(api.calls) == after_first


def test_the_running_month_is_served_but_never_frozen(tmp_path: Path) -> None:
    """Le figer donnerait une série qui paraît complète alors qu'elle se remplit encore."""
    start = month_bounds_ms(2025, 10)[0]
    api = FakeHyperliquid(hourly(start, 5))

    with api.client() as client:
        report = fill_range(client, tmp_path, (2025, 10), (2025, 10), now_ms=ms("2025-10-15T12:00"))

    assert report.not_final == [(2025, 10)]
    assert report.fetched == []
    assert len(report.rows) == 5
    assert not is_cached(tmp_path, 2025, 10)


def test_a_month_the_cache_does_not_hold_is_not_invented(tmp_path: Path) -> None:
    with pytest.raises(CacheMissError, match="absent du cache"):
        read_month(tmp_path, 2025, 10)


def test_a_cached_month_holding_a_foreign_row_is_refused(tmp_path: Path) -> None:
    store_month(tmp_path, 2025, 10, hourly(month_bounds_ms(2025, 9)[0], 2))
    with pytest.raises(HlError, match="hors du mois"):
        read_month(tmp_path, 2025, 10)


# --- La plage -----------------------------------------------------------------


def test_the_range_never_asks_for_months_before_the_first_listing(tmp_path: Path) -> None:
    """Hyperliquid ne cotait pas l'ETH avant le 2023-05 : ces mois-là n'ont pas de funding."""
    api = FakeHyperliquid([])
    with api.client() as client:
        report = fill_range(client, tmp_path, (2021, 1), FIRST_MONTH, now_ms=ms("2026-01-01T00:00"))

    assert [body["startTime"] for body in api.calls] == [month_bounds_ms(*FIRST_MONTH)[0]]
    assert report.fetched == [FIRST_MONTH]


def test_an_empty_month_after_the_launch_is_flagged(tmp_path: Path) -> None:
    """Un mois vide en 2024 n'est pas un mois d'avant le lancement : c'est une anomalie."""
    api = FakeHyperliquid([])
    with api.client() as client:
        report = fill_range(client, tmp_path, (2024, 3), (2024, 3), now_ms=ms("2026-01-01T00:00"))

    assert report.empty_unexpected == [(2024, 3)]


# --- La période d'un versement ------------------------------------------------


def test_the_period_comes_from_a_dated_rule_not_from_the_neighbour() -> None:
    """Sinon les trois heures manquantes de l'historique deviendraient des périodes de 2 h."""
    assert declared_period_hours(HOURLY_SINCE_MS) == 1
    assert declared_period_hours(HOURLY_SINCE_MS - 1) == EIGHT_HOURS
    assert declared_period_hours(ms("2023-05-12T00:00")) == EIGHT_HOURS
    assert declared_period_hours(ms("2025-10-10T22:00")) == 1


def test_the_switch_of_pace_is_not_read_as_a_hole() -> None:
    """Le 2023-06-08, HL passe de 8 h à l'heure. Les deux rythmes se suivent sans trou."""
    before = [
        HlFunding(HOURLY_SINCE_MS - 16 * HOUR_MS, 0.0006, 0.0009),
        HlFunding(HOURLY_SINCE_MS - 8 * HOUR_MS, 0.0003, 0.0006),
    ]
    after = [HlFunding(HOURLY_SINCE_MS + index * HOUR_MS, 0.00001, 0.0003) for index in range(3)]

    common = as_funding(before + after)

    assert intervals(common) == {EIGHT_HOURS: 2, 1: 3}
    assert gaps(common) == []


def test_a_missing_hour_is_still_a_hole() -> None:
    kept = [
        HlFunding(HOURLY_SINCE_MS, 0.00001, 0.0003),
        HlFunding(HOURLY_SINCE_MS + 2 * HOUR_MS, 0.00001, 0.0003),
    ]
    assert gaps(as_funding(kept)) == [(HOURLY_SINCE_MS, 1.0)]
