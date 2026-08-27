"""Micro-op safety guard — the last line of defence before any tx is sent.

Every check here MUST run BEFORE any network call. If a check refuses, the
executor returns without touching the chain. That is the contract: the guard
cannot be bypassed by a mistake in call order.

Rules enforced (README §14 spirit):
- Allowlist: only pre-declared operation kinds are permitted.
- Amount cap: no single op above `config.tracer.max_op_usd`.
- Rate limit: no more than `config.tracer.max_ops_per_hour` in the rolling
  last hour.
- KILL file: any `KILL` at project root refuses new ops (in-flight tx are
  left to confirm — we don't cancel them mid-air).
- First-use confirmation: the first time we execute a specific `op_kind`,
  a token must be provided (CLI flag, env var, or an explicit
  `confirm_kind(kind)` call from the operator).

The guard is stateful (holds the rolling counter and confirmed-kinds set) but
has no I/O beyond checking the KILL file's existence.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from delta0.config import TracerConfig
from delta0.logging import get_logger

log = get_logger(__name__)


class SafetyRefused(Exception):  # noqa: N818 - "Refused" reads better in stack traces than "RefusedError"
    """Raised when the guard refuses a micro-op. Always safe to catch."""


ALLOWED_OP_KINDS: frozenset[str] = frozenset(
    {
        "aave_approve",
        "aave_supply",
        "aave_borrow",
        "aave_repay",
        "aave_withdraw",
        "hl_post_only_cancel",
        "bridge_out",
        "bridge_in",
    },
)


@dataclass(slots=True)
class MicroOpsGuard:
    """Runtime enforcer of the tracer safeties."""

    config: TracerConfig
    project_root: Path
    _op_timestamps: deque[float] = field(default_factory=deque)
    _confirmed_kinds: set[str] = field(default_factory=set)

    def confirm_kind(self, op_kind: str) -> None:
        """Explicitly allow the first execution of `op_kind`.

        Called by the CLI when the operator passes `--confirm supply`
        (for example) or by a test that wants to bypass the manual gate.
        """
        if op_kind not in ALLOWED_OP_KINDS:
            raise ValueError(f"unknown op kind: {op_kind}")
        self._confirmed_kinds.add(op_kind)
        log.info(
            "op_kind_confirmed",
            message=f"première exécution autorisée pour {op_kind}",
            op_kind=op_kind,
        )

    def check(self, op_kind: str, notional_usd: float, now: float | None = None) -> None:
        """Raise `SafetyRefused` if the op cannot proceed. Otherwise register it.

        MUST be called BEFORE any network activity. Callers that swallow the
        exception without acting on it are buggy.
        """
        now = now if now is not None else time.monotonic()

        # 1. Allowlist.
        if op_kind not in ALLOWED_OP_KINDS:
            raise SafetyRefused(f"opération non autorisée: {op_kind!r}")

        # 2. KILL file.
        if (self.project_root / "KILL").exists() or (self.project_root / "KILL_DEFLATE").exists():
            raise SafetyRefused(
                "fichier KILL présent — refus de toute nouvelle micro-op",
            )

        # 3. Amount cap.
        if notional_usd > self.config.max_op_usd:
            raise SafetyRefused(
                f"notionnel {notional_usd:.2f} $ > plafond {self.config.max_op_usd:.2f} $",
            )
        if notional_usd < 0:
            raise SafetyRefused(f"notionnel négatif interdit: {notional_usd}")

        # 4. Rate limit — drop stale timestamps first.
        cutoff = now - 3600.0
        while self._op_timestamps and self._op_timestamps[0] < cutoff:
            self._op_timestamps.popleft()
        if len(self._op_timestamps) >= self.config.max_ops_per_hour:
            raise SafetyRefused(
                f"plafond de fréquence atteint: {len(self._op_timestamps)} "
                f"ops dans la dernière heure (max {self.config.max_ops_per_hour})",
            )

        # 5. First-use confirmation.
        if self.config.require_first_use_confirmation and op_kind not in self._confirmed_kinds:
            raise SafetyRefused(
                f"première exécution de {op_kind!r} nécessite une confirmation opérateur "
                "(CLI --confirm ou guard.confirm_kind)",
            )

        # All checks passed — register.
        self._op_timestamps.append(now)

    def ops_in_last_hour(self, now: float | None = None) -> int:
        """Number of ops that succeeded the guard in the last 3600 s."""
        now = now if now is not None else time.monotonic()
        cutoff = now - 3600.0
        return sum(1 for t in self._op_timestamps if t >= cutoff)
