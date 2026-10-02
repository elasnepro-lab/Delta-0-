"""The local archive cache: five years of minutes, kept off the network and off git.

Three series over five years are roughly 300 MB of public, re-downloadable
archives, so they live under `data/backtest/` — which git ignores — and never
in the repository.

What the cache promises, and why each promise earns its code:

- **a month is written only after its sha256 checks out, and by an atomic
  rename.** An interrupted download leaves a `.partial` file that nothing
  reads, never a short archive that would pass for a complete month. This is
  not a hypothetical: the session that started this module was cut mid-work;
  the failure to design against is the machine stopping at the wrong instant;
- **the `.CHECKSUM` is kept beside the archive.** A month already taken can be
  re-verified from disk alone, without asking Binance anything;
- **a month already present and conforming never touches the network.**
  Resuming an interrupted campaign costs one disk read per month already held.

The checksum is written after the archive, so a crash between the two leaves
the month looking absent — which costs one re-download, the cheap mistake. The
expensive one would be a month looking present when it is not whole.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from backtest.binance import (
    ArchiveError,
    ArchiveMissingError,
    Candle,
    Series,
    archive_name,
    fetch_month_bytes,
    months,
    parse_checksum,
    read_archive,
)

DEFAULT_ROOT = Path("data") / "backtest" / "binance"

_PARTIAL_SUFFIX = ".partial"
_READ_CHUNK = 1 << 20

Month = tuple[int, int]
Progress = Callable[[str, int, int], None]


class CacheMissError(ArchiveError):
    """Asked for a month the cache does not hold, or does not hold whole."""


def month_path(root: Path, series: Series, year: int, month: int) -> Path:
    return root / series.name / archive_name(series, year, month)


def checksum_path(root: Path, series: Series, year: int, month: int) -> Path:
    path = month_path(root, series, year, month)
    return path.with_name(path.name + ".CHECKSUM")


def file_digest(path: Path) -> str:
    """sha256 of a file, read in chunks: a month is a few megabytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def stored_digest(root: Path, series: Series, year: int, month: int) -> str | None:
    """The sha256 kept beside a cached month — None when it is absent or unreadable.

    An unreadable `.CHECKSUM` reads as an absent month on purpose: the month is
    downloaded again, which costs bandwidth, rather than trusted, which would
    cost a hole in the price series.
    """
    path = checksum_path(root, series, year, month)
    if not path.is_file():
        return None
    try:
        return parse_checksum(path.read_text(encoding="ascii"), archive_name(series, year, month))
    except (ArchiveError, UnicodeDecodeError, OSError):
        return None


def is_cached(root: Path, series: Series, year: int, month: int) -> bool:
    """Whole month on disk, matching the checksum stored next to it."""
    expected = stored_digest(root, series, year, month)
    path = month_path(root, series, year, month)
    if expected is None or not path.is_file():
        return False
    return file_digest(path) == expected


def store(root: Path, series: Series, year: int, month: int, payload: bytes, digest: str) -> Path:
    """Write one month and its checksum, each by an atomic rename."""
    path = month_path(root, series, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)

    partial = path.with_name(path.name + _PARTIAL_SUFFIX)
    partial.write_bytes(payload)
    os.replace(partial, path)

    checksum = checksum_path(root, series, year, month)
    partial_checksum = checksum.with_name(checksum.name + _PARTIAL_SUFFIX)
    partial_checksum.write_text(
        f"{digest}  {archive_name(series, year, month)}\n", encoding="ascii"
    )
    os.replace(partial_checksum, checksum)
    return path


def ensure_month(
    client: httpx.Client, root: Path, series: Series, year: int, month: int
) -> tuple[Path, bool]:
    """The month on disk, downloaded if need be. The flag says whether it was fetched."""
    if is_cached(root, series, year, month):
        return month_path(root, series, year, month), False
    payload, digest = fetch_month_bytes(client, series, year, month)
    return store(root, series, year, month, payload, digest), True


def cached_bytes(root: Path, series: Series, year: int, month: int) -> tuple[bytes, str]:
    """The stored archive and the digest it was stored with: the only door into the cache."""
    expected = stored_digest(root, series, year, month)
    path = month_path(root, series, year, month)
    if expected is None or not path.is_file():
        raise CacheMissError(f"{series.name} {year:04d}-{month:02d} absent du cache {root}")
    return path.read_bytes(), expected


def read_month(root: Path, series: Series, year: int, month: int) -> list[Candle]:
    """Candles of a cached month, verified once more against its stored checksum."""
    payload, expected = cached_bytes(root, series, year, month)
    return read_archive(payload, expected, year, month)


@dataclass(slots=True)
class Report:
    """What one range cost, and what it could not get."""

    series: str
    cached: list[Month] = field(default_factory=list)
    downloaded: list[Month] = field(default_factory=list)
    missing: list[Month] = field(default_factory=list)
    bytes_downloaded: int = 0

    @property
    def interior_gaps(self) -> list[Month]:
        """Missing months with a later month present — never 'not published yet'.

        Binance publishes a few days late, so the last month or two of a range
        can be legitimately absent. A hole *inside* the range cannot: it is a
        stretch of history the backtest would silently read as calm.
        """
        present = sorted(self.cached + self.downloaded)
        if not present:
            return list(self.missing)
        return [month for month in self.missing if month < present[-1]]


def ensure_range(
    client: httpx.Client,
    root: Path,
    series: Series,
    start: Month,
    end: Month,
    *,
    progress: Progress | None = None,
) -> Report:
    """Fill the cache for every month of a range, and say what happened to each."""
    report = Report(series=series.name)
    for year, month in months(start, end):
        try:
            path, downloaded = ensure_month(client, root, series, year, month)
        except ArchiveMissingError:
            report.missing.append((year, month))
            if progress is not None:
                progress("missing", year, month)
            continue
        if downloaded:
            report.downloaded.append((year, month))
            report.bytes_downloaded += path.stat().st_size
        else:
            report.cached.append((year, month))
        if progress is not None:
            progress("downloaded" if downloaded else "cached", year, month)
    return report
