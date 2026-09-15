"""Chantier 7.2 — the monthly Binance archives that feed the backtest.

Everything here runs offline: archives are built in memory, and the one test
that goes through `httpx` uses a mock transport. What is pinned is what the
archives really do (formats read on 2026-09-16, see the module docstring of
`backtest/binance.py`) and what must never pass silently: a truncated download,
a month with foreign candles, a series read out of order. A quiet hole in the
price series would read as a calm market — the exact error the backtest cannot
afford.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from backtest.binance import (
    MINUTE_MS,
    SERIES,
    ArchiveError,
    ArchiveMissingError,
    Candle,
    archive_name,
    archive_url,
    fetch_month,
    missing_minutes,
    months,
    parse_checksum,
    parse_klines_csv,
    read_archive,
    to_milliseconds,
)
from tests.binance_archives import OCTOBER_2025_START, build_csv, build_zip, full_month, kline_row

# --- The two formats that move under our feet ---------------------------------


def test_microsecond_timestamps_are_normalised() -> None:
    """Spot files switched to microseconds in 2025: 1760054400000000 is a date, not a bug."""
    assert to_milliseconds(1_760_054_400_000_000) == 1_760_054_400_000
    assert to_milliseconds(1_760_054_400_000) == 1_760_054_400_000


def test_both_timestamp_units_read_as_the_same_minute() -> None:
    in_ms = parse_klines_csv(build_csv(3))
    in_us = parse_klines_csv(build_csv(3, micros=True))
    assert [c.ts_ms for c in in_ms] == [c.ts_ms for c in in_us]


@pytest.mark.parametrize("header", [False, True])
def test_the_header_line_is_skipped_wherever_it_appears(header: bool) -> None:
    """2021 files carry no header, 2025 ones do. Neither is a date we can trust."""
    candles = parse_klines_csv(build_csv(5, header=header))
    assert len(candles) == 5
    assert candles[0] == Candle(
        ts_ms=OCTOBER_2025_START, open=3999.0, high=4002.0, low=3997.0, close=4000.0
    )


def test_an_unreadable_line_stops_everything() -> None:
    with pytest.raises(ArchiveError, match="ligne illisible"):
        parse_klines_csv("1759276800000,4000,pas-un-prix,3997,4000\n")


# --- The checksum, which is what makes a download trustworthy -----------------


def test_the_announced_sha256_is_read() -> None:
    digest = "a" * 64
    assert parse_checksum(f"{digest}  ETHUSDT-1m-2025-10.zip", "ETHUSDT-1m-2025-10.zip") == digest


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("a" * 64 + "  AUTRE.zip", "inattendu"),
        ("a" * 64, "inattendu"),
        ("xyz  ETHUSDT-1m-2025-10.zip", "sha256 invalide"),
        ("z" * 64 + "  ETHUSDT-1m-2025-10.zip", "sha256 invalide"),
    ],
)
def test_a_checksum_file_that_does_not_fit_is_refused(text: str, match: str) -> None:
    with pytest.raises(ArchiveError, match=match):
        parse_checksum(text, "ETHUSDT-1m-2025-10.zip")


def test_a_truncated_download_is_refused() -> None:
    """The whole point: a short file would read as a quiet month."""
    payload, digest = full_month()
    with pytest.raises(ArchiveError, match="tronquée"):
        read_archive(payload[:-20], digest, 2025, 10)


# --- One month, and nothing but that month ------------------------------------


def test_a_complete_month_reads_whole() -> None:
    payload, digest = full_month()
    candles = read_archive(payload, digest, 2025, 10)
    assert len(candles) == 31 * 24 * 60
    assert missing_minutes(candles, 2025, 10) == 0


def test_the_missing_minutes_are_counted_never_filled() -> None:
    csv = build_csv(31 * 24 * 60)
    kept = "".join(line + "\n" for i, line in enumerate(csv.splitlines()) if i not in {10, 11, 12})
    payload = build_zip(kept)
    candles = read_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)
    assert missing_minutes(candles, 2025, 10) == 3


@pytest.mark.parametrize(
    ("csv", "match"),
    [
        (build_csv(3, start=OCTOBER_2025_START - 10 * MINUTE_MS), "hors du mois"),
        (build_csv(2) + kline_row(OCTOBER_2025_START) + "\n", "croissantes"),
        (kline_row(OCTOBER_2025_START + 30_000) + "\n", "hors de la grille"),
    ],
)
def test_candles_that_do_not_belong_to_the_month_are_refused(csv: str, match: str) -> None:
    payload = build_zip(csv)
    with pytest.raises(ArchiveError, match=match):
        read_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)


def test_an_archive_holding_more_than_one_file_is_refused() -> None:
    payload = build_zip(build_csv(3), extra="ETHUSDT-1m-2025-10-bis.csv")
    with pytest.raises(ArchiveError, match="un seul attendu"):
        read_archive(payload, hashlib.sha256(payload).hexdigest(), 2025, 10)


# --- Addressing the archives --------------------------------------------------


def test_the_month_walk_crosses_the_year() -> None:
    assert list(months((2022, 11), (2023, 2))) == [(2022, 11), (2022, 12), (2023, 1), (2023, 2)]
    assert list(months((2023, 5), (2023, 5))) == [(2023, 5)]
    assert list(months((2023, 5), (2023, 4))) == []


def test_the_three_series_point_at_three_real_paths() -> None:
    """Mark prices live under their own archive family, not next to the klines."""
    assert archive_url(SERIES["spot"], 2025, 10).endswith(
        "/spot/monthly/klines/ETHUSDT/1m/ETHUSDT-1m-2025-10.zip"
    )
    assert archive_url(SERIES["futures"], 2021, 5).endswith(
        "/futures/um/monthly/klines/ETHUSDT/1m/ETHUSDT-1m-2021-05.zip"
    )
    assert "monthly/markPriceKlines" in archive_url(SERIES["mark"], 2025, 10)
    assert archive_name(2021, 5) == "ETHUSDT-1m-2021-05.zip"


# --- The one path that goes through the network -------------------------------


def test_a_month_not_published_yet_is_told_apart_from_a_failure() -> None:
    """Binance publishes a few days late; that is not a broken archive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArchiveMissingError, match="pas encore publié"),
    ):
        fetch_month(client, SERIES["spot"], 2026, 9)


def test_a_published_month_comes_back_with_its_verified_digest() -> None:
    payload, digest = full_month()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{digest}  {archive_name(2025, 10)}")
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candles, verified = fetch_month(client, SERIES["spot"], 2025, 10)

    assert verified == digest
    assert len(candles) == 31 * 24 * 60
