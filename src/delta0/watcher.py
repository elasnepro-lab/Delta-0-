"""Watcher — produces a `Snapshot` from live venues (M1)."""

from __future__ import annotations


class Watcher:
    def __init__(self) -> None:
        raise NotImplementedError(
            "Watcher loop will be wired in M1. See README section 3.",
        )
