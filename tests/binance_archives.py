"""Binance archives built in memory, shared by every backtest test.

Same role as `tests/world.py` for the bot: one place decides what an archive
looks like, so two tests cannot drift into measuring two different worlds. The
formats reproduced here — candles and funding alike — are the ones read on real
files on 2026-09-16, see the module docstring of `backtest/binance.py`.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import httpx

from backtest.binance import MINUTE_MS, month_bounds_ms

OCTOBER_2025_START = 1_759_276_800_000  # 2025-10-01T00:00:00Z, in milliseconds
HEADER = "open_time,open,high,low,close,volume,close_time"
CSV_NAME = "ETHUSDT-1m-2025-10.csv"

CANDLES_PER_MONTH = 3  # enough to read back; the cache never parses what it stores

FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"
FUNDING_ROWS_PER_MONTH = 3
FUNDING_DRIFT_MS = 7  # real archives carry a few ms of drift: 1761926400007
HOUR_MS = 3_600_000


def kline_row(ts: int, close: float = 4000.0) -> str:
    """One archive line: the five fields we read, then the ones we ignore."""
    ignored = f"12.5,{ts + MINUTE_MS - 1},50000,120,6.2,24000,0"
    return f"{ts},{close - 1},{close + 2},{close - 3},{close},{ignored}"


def build_csv(
    count: int, *, start: int = OCTOBER_2025_START, header: bool = False, micros: bool = False
) -> str:
    rows = [kline_row((start + i * MINUTE_MS) * (1000 if micros else 1)) for i in range(count)]
    return "\n".join(([HEADER] if header else []) + rows) + "\n"


def build_zip(csv: str, *, name: str = CSV_NAME, extra: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv)
        if extra is not None:
            archive.writestr(extra, csv)
    return buffer.getvalue()


def month_archive(year: int = 2025, month: int = 10, *, candles: int | None = None) -> bytes:
    """One well-formed candle archive for that month — complete unless `candles` says otherwise."""
    start, end = month_bounds_ms(year, month)
    count = (end - start) // MINUTE_MS if candles is None else candles
    return build_zip(build_csv(count, start=start))


def full_month(year: int = 2025, month: int = 10) -> tuple[bytes, str]:
    """A complete, well-formed archive for that month, and its true sha256."""
    payload = month_archive(year, month)
    return payload, hashlib.sha256(payload).hexdigest()


def build_funding_csv(
    rows: int, *, start: int, interval_hours: int = 8, drift_ms: int = FUNDING_DRIFT_MS
) -> str:
    """Funding lines, header included — every real archive carries one."""
    step = interval_hours * HOUR_MS
    lines = [FUNDING_HEADER]
    lines += [
        f"{start + index * step + drift_ms},{interval_hours},{-0.00001572 + index * 1e-6:.8f}"
        for index in range(rows)
    ]
    return "\n".join(lines) + "\n"


def funding_archive(
    year: int = 2025,
    month: int = 10,
    *,
    rows: int = FUNDING_ROWS_PER_MONTH,
    interval_hours: int = 8,
) -> bytes:
    start = month_bounds_ms(year, month)[0]
    csv = build_funding_csv(rows, start=start, interval_hours=interval_hours)
    return build_zip(csv, name=f"ETHUSDT-fundingRate-{year:04d}-{month:02d}.csv")


class FakeBinance:
    """The archive server, in memory: it counts requests and can lie on demand.

    Shared by the cache, funding and CLI tests, so all of them measure the same
    server: one that answers 404 for the months in `missing`, and cuts twenty
    bytes off every archive when `truncate` is set. Which family it serves is
    decided by the file name asked for, exactly as data.binance.vision does.
    """

    def __init__(
        self,
        *,
        missing: tuple[tuple[int, int], ...] = (),
        truncate: bool = False,
        candles: int = CANDLES_PER_MONTH,
        funding_rows: int = FUNDING_ROWS_PER_MONTH,
    ) -> None:
        self.missing = set(missing)
        self.truncate = truncate
        self.candles = candles
        self.funding_rows = funding_rows
        self.requests: list[str] = []

    def payload(self, year: int, month: int, *, funding: bool = False) -> bytes:
        if funding:
            return funding_archive(year, month, rows=self.funding_rows)
        return month_archive(year, month, candles=self.candles)

    def digest(self, year: int, month: int, *, funding: bool = False) -> str:
        return hashlib.sha256(self.payload(year, month, funding=funding)).hexdigest()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        asked = request.url.path.split("/")[-1]
        archive = asked.removesuffix(".CHECKSUM")
        year, month = (int(part) for part in archive[-11:-4].split("-"))
        if (year, month) in self.missing:
            return httpx.Response(404)

        body = self.payload(year, month, funding="fundingRate" in archive)
        if asked.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{hashlib.sha256(body).hexdigest()}  {archive}")
        return httpx.Response(200, content=body[:-20] if self.truncate else body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
