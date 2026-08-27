"""Boot-time reconciliation between on-chain state and the SQLite journal.

Per README §13:
  "Au démarrage : réconciliation complète (soldes on-chain + état HL) contre
   le journal ; toute intention `sent` non retrouvée on-chain est investiguée
   avant toute nouvelle action."

M1 scope: read-only comparison. We log warnings on mismatch, but the bot never
acts (M1 = no execution). Anything a human should look at gets a WARN or
CRITICAL level.
"""

from __future__ import annotations

from dataclasses import dataclass

from delta0.logging import get_logger
from delta0.state import StateStore
from delta0.types import Snapshot

log = get_logger(__name__)

# An anchor drift above this magnitude at boot is worth a human look
# (typical daily moves are well under it).
_ANCHOR_DRIFT_ALERT = 0.15
# HF below this is worth surfacing as CRITICAL — 1.10 leaves ~5 pt to LT.
_HF_ALERT_FLOOR = 1.10


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of the boot reconciliation pass."""

    anchor_journal: float | None
    anchor_current_mark: float
    anchor_drift_pct: float | None  # None if no anchor recorded yet
    debt_on_chain: float
    debt_last_recorded: float | None
    warnings: tuple[str, ...]


async def reconcile_at_boot(store: StateStore, snapshot: Snapshot) -> ReconcileReport:
    """Compare persistent state to on-chain reality; log any drift."""
    warnings: list[str] = []

    # --- Anchor drift ---------------------------------------------------------
    anchor_str = await store.kv_get("anchor_price")
    anchor: float | None = float(anchor_str) if anchor_str else None
    drift = (snapshot.mark_price - anchor) / anchor if anchor is not None and anchor > 0 else None

    if anchor is None:
        log.info(
            "reconcile_no_anchor",
            message="aucune ancre journalisée — premier boot ou état non initialisé",
        )
    elif drift is not None and abs(drift) > _ANCHOR_DRIFT_ALERT:
        w = (
            f"dérive d'ancre significative : {drift:+.2%} depuis le dernier "
            f"re-centrage (ancre={anchor}, mark={snapshot.mark_price})"
        )
        warnings.append(w)
        log.warning("reconcile_anchor_drift", message=w, drift=drift)

    # --- Debt drift -----------------------------------------------------------
    debt_str = await store.kv_get("debt_usd_last")
    debt_last: float | None = float(debt_str) if debt_str else None
    if debt_last is not None:
        gap = snapshot.debt_usd - debt_last
        if abs(gap) > max(50.0, 0.05 * max(debt_last, 1.0)):
            w = (
                f"dette on-chain ({snapshot.debt_usd:.0f} $) diverge du dernier "
                f"snapshot journalisé ({debt_last:.0f} $) — gap {gap:+.0f} $"
            )
            warnings.append(w)
            log.warning("reconcile_debt_drift", message=w, gap=gap)

    # --- HF sanity ------------------------------------------------------------
    if snapshot.hf < _HF_ALERT_FLOOR and snapshot.debt_usd > 0:
        w = f"HF observé {snapshot.hf:.4f} sous {_HF_ALERT_FLOOR} — position à surveiller"
        warnings.append(w)
        log.critical("reconcile_hf_low", message=w, hf=snapshot.hf)

    # --- e-mode sanity --------------------------------------------------------
    if snapshot.aave_emode != 0:
        w = f"e-mode Aave = {snapshot.aave_emode} (attendu 0 — README §8.1)"
        warnings.append(w)
        log.warning("reconcile_emode_nonzero", message=w, emode=snapshot.aave_emode)

    # Update last-known debt for next boot.
    await store.kv_set("debt_usd_last", f"{snapshot.debt_usd:.2f}")

    return ReconcileReport(
        anchor_journal=anchor,
        anchor_current_mark=snapshot.mark_price,
        anchor_drift_pct=drift,
        debt_on_chain=snapshot.debt_usd,
        debt_last_recorded=debt_last,
        warnings=tuple(warnings),
    )
