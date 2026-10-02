"""Chantier 7.2 — the local archive cache, and what it refuses to call a month.

No test here reaches the network: a mock transport plays Binance and counts
every request, which is how "already cached costs nothing" gets measured rather
than asserted. The cases that matter are the ugly ones — a download cut in
half, a checksum missing, a file rotting on disk — because each of them, taken
for a good month, would put a silent hole in the price series.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from backtest.binance import SERIES, ArchiveError, Series, month_bounds_ms
from backtest.cache import (
    DEFAULT_ROOT,
    CacheMissError,
    checksum_path,
    ensure_month,
    ensure_range,
    file_digest,
    is_cached,
    month_path,
    read_month,
    store,
    stored_digest,
)
from tests.binance_archives import CANDLES_PER_MONTH, FakeBinance, month_archive

SPOT = SERIES["spot"]


@pytest.fixture
def binance() -> FakeBinance:
    return FakeBinance()


def take(client: httpx.Client, root: Path, year: int = 2025, month: int = 10) -> tuple[Path, bool]:
    return ensure_month(client, root, SPOT, year, month)


# --- What the cache is for ----------------------------------------------------


def test_the_cache_lives_where_git_does_not_look() -> None:
    """Three series over five years are ~300 MB of re-downloadable public data."""
    assert DEFAULT_ROOT.parts[0] == "data"  # `data/` is in .gitignore


def test_a_month_is_downloaded_once_and_never_again(tmp_path: Path, binance: FakeBinance) -> None:
    with binance.client() as client:
        path, downloaded = take(client, tmp_path)
        assert downloaded
        first_pass = len(binance.requests)

        again, downloaded_again = take(client, tmp_path)

    assert (again, downloaded_again) == (path, False)
    assert len(binance.requests) == first_pass, "un mois déjà pris a repassé par le réseau"


def test_the_cached_month_reads_back_as_candles(tmp_path: Path, binance: FakeBinance) -> None:
    with binance.client() as client:
        take(client, tmp_path)

    candles = read_month(tmp_path, SPOT, 2025, 10)
    assert len(candles) == CANDLES_PER_MONTH
    assert candles[0].ts_ms == month_bounds_ms(2025, 10)[0]


def test_the_checksum_is_kept_beside_the_archive(tmp_path: Path, binance: FakeBinance) -> None:
    """So a month already taken can be re-verified without asking Binance anything."""
    with binance.client() as client:
        path, _ = take(client, tmp_path)

    kept = stored_digest(tmp_path, SPOT, 2025, 10)
    assert kept == file_digest(path) == binance.digest(2025, 10)
    assert checksum_path(tmp_path, SPOT, 2025, 10).is_file()


# --- What it refuses to call a month ------------------------------------------


def test_a_month_the_cache_does_not_hold_is_not_invented(tmp_path: Path) -> None:
    assert not is_cached(tmp_path, SPOT, 2025, 10)
    with pytest.raises(CacheMissError, match="absent du cache"):
        read_month(tmp_path, SPOT, 2025, 10)


def test_an_archive_that_rotted_on_disk_is_not_taken_for_a_good_one(
    tmp_path: Path, binance: FakeBinance
) -> None:
    with binance.client() as client:
        path, _ = take(client, tmp_path)
        path.write_bytes(path.read_bytes()[:-20])

        assert not is_cached(tmp_path, SPOT, 2025, 10)
        with pytest.raises(ArchiveError, match="tronquée"):
            read_month(tmp_path, SPOT, 2025, 10)

        _, downloaded = take(client, tmp_path)

    assert downloaded, "un mois corrompu doit être repris, pas gardé"
    assert is_cached(tmp_path, SPOT, 2025, 10)


@pytest.mark.parametrize("content", [None, "pas une empreinte", "z" * 64 + "  AUTRE.zip"])
def test_a_checksum_that_cannot_be_trusted_makes_the_month_absent(
    tmp_path: Path, binance: FakeBinance, content: str | None
) -> None:
    """Costing a re-download is the cheap mistake; trusting the file is the expensive one."""
    with binance.client() as client:
        take(client, tmp_path)
        checksum = checksum_path(tmp_path, SPOT, 2025, 10)
        if content is None:
            checksum.unlink()
        else:
            checksum.write_text(content, encoding="ascii")

        assert stored_digest(tmp_path, SPOT, 2025, 10) is None
        assert not is_cached(tmp_path, SPOT, 2025, 10)

        _, downloaded = take(client, tmp_path)

    assert downloaded
    assert is_cached(tmp_path, SPOT, 2025, 10)


def test_a_truncated_download_leaves_nothing_behind(tmp_path: Path) -> None:
    """Refused before the write: the cache never holds a short month, not even briefly."""
    server = FakeBinance(truncate=True)
    with server.client() as client, pytest.raises(ArchiveError, match="tronqué"):
        take(client, tmp_path)

    assert not month_path(tmp_path, SPOT, 2025, 10).exists()
    assert list(tmp_path.rglob("*" + ".partial")) == []


def test_an_interrupted_write_is_never_read_as_a_month(tmp_path: Path) -> None:
    """A `.partial` left by a machine that stopped mid-write is not a month."""
    path = month_path(tmp_path, SPOT, 2025, 10)
    path.parent.mkdir(parents=True)
    path.with_name(path.name + ".partial").write_bytes(month_archive(2025, 10, candles=1)[:40])

    assert not is_cached(tmp_path, SPOT, 2025, 10)
    with pytest.raises(CacheMissError):
        read_month(tmp_path, SPOT, 2025, 10)


def test_a_month_written_by_hand_is_read_like_any_other(tmp_path: Path) -> None:
    """`store` is the whole contract between a download and the disk."""
    payload = month_archive(2023, 6, candles=CANDLES_PER_MONTH)
    digest = hashlib.sha256(payload).hexdigest()

    store(tmp_path, SPOT, 2023, 6, payload, digest)

    assert is_cached(tmp_path, SPOT, 2023, 6)
    assert len(read_month(tmp_path, SPOT, 2023, 6)) == CANDLES_PER_MONTH


# --- A whole range, and the holes in it ---------------------------------------


def test_a_range_says_what_it_took_and_what_it_could_not(tmp_path: Path) -> None:
    """The last month or two can be legitimately absent: Binance publishes late."""
    server = FakeBinance(missing=((2025, 11),))
    seen: list[tuple[str, int, int]] = []
    with server.client() as client:
        report = ensure_range(
            client,
            tmp_path,
            SPOT,
            (2025, 9),
            (2025, 11),
            progress=lambda state, year, month: seen.append((state, year, month)),
        )

    assert report.downloaded == [(2025, 9), (2025, 10)]
    assert report.missing == [(2025, 11)]
    assert report.cached == []
    assert report.bytes_downloaded > 0
    assert report.interior_gaps == [], "un mois non encore publié n'est pas un trou"
    assert seen == [("downloaded", 2025, 9), ("downloaded", 2025, 10), ("missing", 2025, 11)]


def test_a_hole_inside_the_range_is_not_a_late_publication(tmp_path: Path) -> None:
    """This is the one the backtest would otherwise read as a calm month."""
    server = FakeBinance(missing=((2025, 10),))
    with server.client() as client:
        report = ensure_range(client, tmp_path, SPOT, (2025, 9), (2025, 11))

    assert report.missing == [(2025, 10)]
    assert report.interior_gaps == [(2025, 10)]


def test_resuming_a_range_costs_no_request(tmp_path: Path, binance: FakeBinance) -> None:
    """The point of the cache: a campaign cut in half resumes on disk reads alone."""
    with binance.client() as client:
        first = ensure_range(client, tmp_path, SPOT, (2025, 9), (2025, 11))
        after_first = len(binance.requests)

        second = ensure_range(client, tmp_path, SPOT, (2025, 9), (2025, 11))

    assert len(first.downloaded) == 3
    assert len(second.cached) == 3
    assert second.downloaded == []
    assert second.bytes_downloaded == 0
    assert len(binance.requests) == after_first


def test_each_series_gets_its_own_shelf(tmp_path: Path, binance: FakeBinance) -> None:
    """Spot and mark publish an archive under the same NAME; only the path differs."""
    mark: Series = SERIES["mark"]
    assert month_path(tmp_path, SPOT, 2025, 10) != month_path(tmp_path, mark, 2025, 10)

    with binance.client() as client:
        ensure_month(client, tmp_path, SPOT, 2025, 10)
        ensure_month(client, tmp_path, mark, 2025, 10)

    assert is_cached(tmp_path, SPOT, 2025, 10)
    assert is_cached(tmp_path, mark, 2025, 10)
