"""Binance archives built in memory, shared by every backtest test.

Same role as `tests/world.py` for the bot: one place decides what an archive
looks like, so two tests cannot drift into measuring two different worlds. The
formats reproduced here are the ones read on real files on 2026-09-16 — see the
module docstring of `backtest/binance.py`.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import httpx

from backtest.binance import MINUTE_MS, archive_name, month_bounds_ms

CANDLES_PER_MONTH = 3  # enough to read back; the cache never parses what it stores

OCTOBER_2025_START = 1_759_276_800_000  # 2025-10-01T00:00:00Z, in milliseconds
HEADER = "open_time,open,high,low,close,volume,close_time"
CSV_NAME = "ETHUSDT-1m-2025-10.csv"


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
    """One well-formed archive for that month — complete unless `candles` says otherwise."""
    start, end = month_bounds_ms(year, month)
    count = (end - start) // MINUTE_MS if candles is None else candles
    return build_zip(build_csv(count, start=start))


def full_month(year: int = 2025, month: int = 10) -> tuple[bytes, str]:
    """A complete, well-formed archive for that month, and its true sha256."""
    payload = month_archive(year, month)
    return payload, hashlib.sha256(payload).hexdigest()


class FakeBinance:
    """The archive server, in memory: it counts requests and can lie on demand.

    Shared by the cache and the CLI tests, so both measure the same server: one
    that answers 404 for the months in `missing`, and cuts twenty bytes off
    every archive when `truncate` is set.
    """

    def __init__(
        self,
        *,
        missing: tuple[tuple[int, int], ...] = (),
        truncate: bool = False,
        candles: int = CANDLES_PER_MONTH,
    ) -> None:
        self.missing = set(missing)
        self.truncate = truncate
        self.candles = candles
        self.requests: list[str] = []

    def payload(self, year: int, month: int) -> bytes:
        return month_archive(year, month, candles=self.candles)

    def digest(self, year: int, month: int) -> str:
        return hashlib.sha256(self.payload(year, month)).hexdigest()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        name = request.url.path.split("/")[-1]
        year, month = (int(part) for part in name.removesuffix(".CHECKSUM")[-11:-4].split("-"))
        if (year, month) in self.missing:
            return httpx.Response(404)
        if name.endswith(".CHECKSUM"):
            return httpx.Response(
                200, text=f"{self.digest(year, month)}  {archive_name(year, month)}"
            )
        body = self.payload(year, month)
        return httpx.Response(200, content=body[:-20] if self.truncate else body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
