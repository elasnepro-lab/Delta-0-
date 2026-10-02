"""Binance monthly archives: the price and funding series of the backtest (README §15.3).

Four ETHUSDT series from data.binance.vision — three of one-minute candles, one
of funding rates:

- `spot`    : proxy for the Chainlink ETH/USD behind Aave's oracle;
- `futures` : last traded price on the USDⓈ-M perpetual, kept for the wick studies;
- `mark`    : futures mark price, proxy for Hyperliquid's mark, which has no
              public history (docs/backtest/inventaire-prix-funding.md);
- `funding` : what the perpetual actually charged, the proxy for Hyperliquid's
              own funding over the years when Hyperliquid did not exist.

The formats were read from the files themselves on 2026-09-16, and two of them
move under your feet:

- spot timestamps switch from milliseconds to MICROSECONDS in 2025
  (`1760054400000000` on 2025-10-10);
- futures and mark files carry no header in 2021, and a header line
  (`open_time,open,...`) by 2025.

So the parser normalises every timestamp to milliseconds by its magnitude and
skips a header wherever one appears, rather than trusting a date for either
change. Every archive has a `.CHECKSUM` sibling, `<sha256>  <file name>`, and a
download that does not match it is refused: a truncated month would read as a
calm one.

The funding archives are a family of their own, read on 2026-09-16 too: no
interval folder in their path, a header of their own, and — the useful part —
`funding_interval_hours` written on every row, so the period a rate covers is
read rather than assumed. Their timestamps carry a few milliseconds of drift
(`1761926400007`), so they sit on no grid at all. They start in 2020-01.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

ARCHIVE_BASE = "https://data.binance.vision/data"
SYMBOL = "ETHUSDT"
MINUTE_MS = 60_000

# A millisecond epoch has 13 digits until the year 2286; a microsecond one has 16.
_MICROSECOND_FLOOR = 10**15

_MONTHS_IN_YEAR = 12
_CHECKSUM_FIELDS = 2  # `<sha256>  <file name>`, and nothing else
_SHA256_HEX_LEN = 64


class ArchiveError(Exception):
    """An archive that cannot be trusted as a complete record of its month."""


class ArchiveMissingError(ArchiveError):
    """No archive published for this month yet (Binance publishes a few days late)."""


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    market: str  # path segment under ARCHIVE_BASE
    kind: str  # archive family
    marker: str  # what the file name carries between the symbol and the month
    nested: bool  # candles live under an extra interval folder, funding does not

    @property
    def is_candles(self) -> bool:
        """Funding archives hold rates, not candles, and parse differently."""
        return self.kind != FUNDING_KIND


FUNDING_KIND = "fundingRate"

SERIES: dict[str, Series] = {
    "spot": Series("spot", "spot", "klines", "1m", nested=True),
    "futures": Series("futures", "futures/um", "klines", "1m", nested=True),
    "mark": Series("mark", "futures/um", "markPriceKlines", "1m", nested=True),
    "funding": Series("funding", "futures/um", FUNDING_KIND, FUNDING_KIND, nested=False),
}


@dataclass(frozen=True, slots=True)
class Candle:
    ts_ms: int  # open time, milliseconds since the epoch, UTC
    open: float
    high: float
    low: float
    close: float


def archive_name(series: Series, year: int, month: int) -> str:
    return f"{SYMBOL}-{series.marker}-{year:04d}-{month:02d}.zip"


def archive_url(series: Series, year: int, month: int) -> str:
    folder = f"{ARCHIVE_BASE}/{series.market}/monthly/{series.kind}/{SYMBOL}"
    if series.nested:
        folder = f"{folder}/{series.marker}"
    return f"{folder}/{archive_name(series, year, month)}"


def months(start: tuple[int, int], end: tuple[int, int]) -> Iterator[tuple[int, int]]:
    """Every (year, month) from `start` to `end`, both included."""
    year, month = start
    while (year, month) <= end:
        yield year, month
        year, month = (year + 1, 1) if month == _MONTHS_IN_YEAR else (year, month + 1)


def month_bounds_ms(year: int, month: int) -> tuple[int, int]:
    """First minute of the month, and the first minute of the next one."""
    start = calendar.timegm((year, month, 1, 0, 0, 0)) * 1000
    days = calendar.monthrange(year, month)[1]
    return start, start + days * 24 * 60 * MINUTE_MS


def to_milliseconds(raw: int) -> int:
    return raw // 1000 if raw >= _MICROSECOND_FLOOR else raw


def parse_klines_csv(text: str) -> list[Candle]:
    """Candles out of one archive's CSV, header or not, ms or µs."""
    candles: list[Candle] = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue  # blank line, or the header some files carry
        fields = line.split(",")
        try:
            candles.append(
                Candle(
                    ts_ms=to_milliseconds(int(fields[0])),
                    open=float(fields[1]),
                    high=float(fields[2]),
                    low=float(fields[3]),
                    close=float(fields[4]),
                ),
            )
        except (IndexError, ValueError) as e:
            raise ArchiveError(f"ligne illisible : {line[:80]!r}") from e
    return candles


def parse_checksum(text: str, expected_name: str) -> str:
    """The sha256 announced for `expected_name` by its `.CHECKSUM` file."""
    parts = text.split()
    if len(parts) != _CHECKSUM_FIELDS or parts[1] != expected_name:
        raise ArchiveError(f"fichier CHECKSUM inattendu pour {expected_name} : {text[:120]!r}")
    digest = parts[0].lower()
    if len(digest) != _SHA256_HEX_LEN or any(c not in "0123456789abcdef" for c in digest):
        raise ArchiveError(f"empreinte sha256 invalide pour {expected_name} : {digest!r}")
    return digest


def verified_text(zip_bytes: bytes, expected_sha256: str) -> str:
    """The single CSV held by an archive whose sha256 is the one announced for it."""
    actual = hashlib.sha256(zip_bytes).hexdigest()
    if actual != expected_sha256:
        raise ArchiveError(f"sha256 {actual} ≠ annoncé {expected_sha256} : archive tronquée ?")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ArchiveError(f"archive à {len(names)} fichiers, un seul attendu : {names}")
        return archive.read(names[0]).decode("ascii")


def read_archive(zip_bytes: bytes, expected_sha256: str, year: int, month: int) -> list[Candle]:
    """Verify, unzip and parse one monthly archive; refuse what does not fit its month."""
    candles = parse_klines_csv(verified_text(zip_bytes, expected_sha256))
    start, end = month_bounds_ms(year, month)
    previous = start - MINUTE_MS
    for candle in candles:
        if candle.ts_ms % MINUTE_MS:
            raise ArchiveError(f"bougie hors de la grille minute : {candle.ts_ms}")
        if not start <= candle.ts_ms < end:
            raise ArchiveError(f"bougie {candle.ts_ms} hors du mois {year:04d}-{month:02d}")
        if candle.ts_ms <= previous:
            raise ArchiveError(f"bougies non strictement croissantes à {candle.ts_ms}")
        previous = candle.ts_ms
    return candles


def missing_minutes(candles: list[Candle], year: int, month: int) -> int:
    """Minutes of the month without a candle. Read, never silently filled."""
    start, end = month_bounds_ms(year, month)
    return (end - start) // MINUTE_MS - len(candles)


@dataclass(frozen=True, slots=True)
class Funding:
    ts_ms: int  # calc_time: the instant the rate was charged, UTC
    interval_hours: int  # written on the row, never assumed
    rate: float  # over that whole interval, not per hour


def parse_funding_csv(text: str) -> list[Funding]:
    """Rows out of `calc_time,funding_interval_hours,last_funding_rate`."""
    rows: list[Funding] = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue  # blank line, or the header
        fields = line.split(",")
        try:
            rows.append(
                Funding(
                    ts_ms=to_milliseconds(int(fields[0])),
                    interval_hours=int(fields[1]),
                    rate=float(fields[2]),
                ),
            )
        except (IndexError, ValueError) as e:
            raise ArchiveError(f"ligne de funding illisible : {line[:80]!r}") from e
    return rows


def read_funding_archive(
    zip_bytes: bytes, expected_sha256: str, year: int, month: int
) -> list[Funding]:
    """Verify and parse one month of funding; refuse what does not belong to it.

    No minute grid is checked here: funding timestamps drift by a few
    milliseconds, so only the month, the order and the declared interval are.
    """
    rows = parse_funding_csv(verified_text(zip_bytes, expected_sha256))
    start, end = month_bounds_ms(year, month)
    previous = -1
    for row in rows:
        if not start <= row.ts_ms < end:
            raise ArchiveError(f"funding {row.ts_ms} hors du mois {year:04d}-{month:02d}")
        if row.ts_ms <= previous:
            raise ArchiveError(f"funding non strictement croissant à {row.ts_ms}")
        if row.interval_hours <= 0:
            raise ArchiveError(f"intervalle de funding impossible : {row.interval_hours} h")
        previous = row.ts_ms
    return rows


def fetch_month_bytes(
    client: httpx.Client, series: Series, year: int, month: int
) -> tuple[bytes, str]:
    """Download one month's archive, checked against the sha256 its `.CHECKSUM` announces.

    The bytes come back unparsed, so the cache can store the archive exactly as
    published — and store nothing at all when the download does not check out.
    """
    url = archive_url(series, year, month)
    checksum = client.get(f"{url}.CHECKSUM")
    if checksum.status_code == httpx.codes.NOT_FOUND:
        raise ArchiveMissingError(f"{series.name} {year:04d}-{month:02d} pas encore publié")
    checksum.raise_for_status()
    expected = parse_checksum(checksum.text, archive_name(series, year, month))

    body = client.get(url)
    body.raise_for_status()
    actual = hashlib.sha256(body.content).hexdigest()
    if actual != expected:
        raise ArchiveError(f"sha256 {actual} ≠ annoncé {expected} : téléchargement tronqué ?")
    return body.content, expected


def fetch_month(
    client: httpx.Client, series: Series, year: int, month: int
) -> tuple[list[Candle], str]:
    """Download one month of one series. Returns the candles and the verified sha256."""
    payload, expected = fetch_month_bytes(client, series, year, month)
    return read_archive(payload, expected, year, month), expected
