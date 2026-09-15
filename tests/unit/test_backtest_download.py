"""Chantier 7.2 — la commande qui peuple le cache, et ce qu'elle refuse de taire.

Aucun test ne touche au réseau. Ce qui est vérifié ici n'est pas l'affichage
mais la distinction que la commande doit tenir : un mois pas encore publié en
fin de plage est normal, un mois manquant au milieu est un trou que le backtest
lirait comme un marché calme — et seul le second doit faire échouer la commande.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
from pathlib import Path

import pytest

from backtest.binance import SERIES
from backtest.cache import is_cached, store
from backtest.download import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_USAGE,
    download,
    last_published_month,
    main,
    parse_month,
)
from tests.binance_archives import FakeBinance, build_csv, build_zip

SPOT = SERIES["spot"]


def run(
    server: FakeBinance,
    root: Path,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    verify_months: bool = False,
) -> tuple[int, str]:
    out = io.StringIO()
    with server.client() as client:
        code = download(client, root, [SPOT], start, end, verify_months=verify_months, out=out)
    return code, out.getvalue()


# --- Reading the arguments ----------------------------------------------------


def test_a_month_is_read_or_refused() -> None:
    assert parse_month("2021-01") == (2021, 1)
    assert parse_month("2025-12") == (2025, 12)
    for bad in ("2021", "2021-13", "janvier", "2021-1-1"):
        with pytest.raises(argparse.ArgumentTypeError, match="AAAA-MM"):
            parse_month(bad)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (dt.date(2026, 1, 3), (2025, 12)),
        (dt.date(2026, 9, 16), (2026, 8)),
        (dt.date(2026, 12, 31), (2026, 11)),
    ],
)
def test_the_range_stops_at_the_last_published_month(
    today: dt.date, expected: tuple[int, int]
) -> None:
    """L'archive du mois courant n'existe pas encore : la demander serait une absence garantie."""
    assert last_published_month(today) == expected


def test_an_empty_range_is_refused_before_any_request() -> None:
    assert main(["--from", "2025-10", "--to", "2025-09"]) == EXIT_USAGE


def test_all_is_the_three_series(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_download(client, root, series, start, end, **kwargs):  # type: ignore[no-untyped-def]
        seen.append([one.name for one in series])
        return EXIT_OK

    monkeypatch.setattr("backtest.download.download", fake_download)
    assert main(["--to", "2021-01"]) == EXIT_OK
    assert main(["--series", "mark", "--to", "2021-01"]) == EXIT_OK
    assert seen == [["spot", "futures", "mark"], ["mark"]]


# --- Filling the cache --------------------------------------------------------


def test_a_range_is_downloaded_then_never_downloaded_again(tmp_path: Path) -> None:
    server = FakeBinance()

    code, first = run(server, tmp_path, (2025, 9), (2025, 11))
    after_first = len(server.requests)
    again, second = run(server, tmp_path, (2025, 9), (2025, 11))

    assert (code, again) == (EXIT_OK, EXIT_OK)
    assert "3 pris" in first
    assert "3 déjà en cache" in second
    assert len(server.requests) == after_first, "une relance a repassé par le réseau"
    assert all(is_cached(tmp_path, SPOT, 2025, month) for month in (9, 10, 11))


def test_a_month_not_published_yet_does_not_fail_the_command(tmp_path: Path) -> None:
    """Binance publie avec quelques jours de retard : la fin de plage peut manquer."""
    server = FakeBinance(missing=((2025, 11),))

    code, output = run(server, tmp_path, (2025, 9), (2025, 11))

    assert code == EXIT_OK
    assert "absent 2025-11" in output
    assert "TROU" not in output


def test_a_hole_inside_the_range_fails_the_command(tmp_path: Path) -> None:
    """Celui-là ne s'explique pas, et le backtest le lirait comme un mois calme."""
    server = FakeBinance(missing=((2025, 10),))

    code, output = run(server, tmp_path, (2025, 9), (2025, 11))

    assert code == EXIT_INCOMPLETE
    assert "TROU dans la plage : 2025-10" in output


# --- Verification -------------------------------------------------------------


def test_the_verification_counts_the_missing_minutes(tmp_path: Path) -> None:
    csv = build_csv(31 * 24 * 60)
    pierced = "".join(
        line + "\n" for index, line in enumerate(csv.splitlines()) if index not in {5, 6, 7}
    )
    payload = build_zip(pierced)
    store(tmp_path, SPOT, 2025, 10, payload, hashlib.sha256(payload).hexdigest())
    server = FakeBinance()

    code, output = run(server, tmp_path, (2025, 10), (2025, 10), verify_months=True)

    assert code == EXIT_OK
    assert server.requests == [], "un mois déjà en cache ne se revérifie pas par le réseau"
    assert "3 minute(s) manquante(s)" in output
    assert "2025-10 (3)" in output


def test_the_verification_says_so_when_nothing_is_missing(tmp_path: Path) -> None:
    server = FakeBinance(candles=31 * 24 * 60)

    code, output = run(server, tmp_path, (2025, 10), (2025, 10), verify_months=True)

    assert code == EXIT_OK
    assert "aucune minute manquante" in output
