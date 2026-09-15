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

from backtest.binance import MINUTE_MS, month_bounds_ms

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
