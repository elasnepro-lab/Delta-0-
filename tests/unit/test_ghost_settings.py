"""Chantier 6.6 — no setting exists without a reader, unless it names its chantier.

M1 found four ghosts by accident: `ARBITRUM_RPC_FALLBACK`, `TG_TOKEN`, `TG_CHAT`
and `anchor_price`. Each one was declared, validated, documented — and read or
written by nothing, so it gave the illusion of a protection that did not
exist. Finding them by accident does not scale; this test makes the next one
impossible to add silently.

A setting may legitimately wait for the code that will consume it. It then
goes in `AWAITING_READER` with the chantier that will read it, and the test
fails the day it gains a reader, so the registry cannot rot into a list of
excuses. State keys follow the same rule: a key read must be written somewhere.

Detection is textual — an attribute access `.name` in `src/delta0`, outside
`config/` and `settings.py`, which declare but do not consume. It can be fooled
by an unrelated attribute of the same name; it cannot miss a setting that
nothing mentions at all, which is the failure it exists for.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from delta0.config.schema import AlertsConfig, Config
from delta0.settings import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "delta0"

# Declared and validated, deliberately not consumed yet.
AWAITING_READER: dict[str, str] = {
    "exposure_mult_half": "8.6 — porte de régime, exposition intermédiaire",
    "maintenance_margin": "8.4 — comparaison au démarrage avec la maintenance lue via l'API",
    "cushion_floor_pct": "6.1 — invariant I4, puis 8.5 reconstitution du coussin",
    "skim_policy": "8.5 — écrémage-recomposition",
    "regime.spread_full_bps": "8.6 — porte de régime",
    "regime.hysteresis_days": "8.6 — porte de régime",
    "slippage_max_bps": "8.1 — agrégateur de swap",
    "order_style": "8.3 — ordres maker puis traversée du spread",
    "gas_min_eth": "6.1 — invariant I5",
    "venues.aave_data_provider": (
        "8.4 — LT de la réserve lu avant toute position : getUserAccountData rend 0 "
        "sur un compte vide"
    ),
    "live_small_cap_pct": "8.4 — plafond de capital en LIVE_SMALL",
    "env.hl_agent_address": "5.3 — deux signataires ; M1 signe avec la clé maître",
    "env.hl_agent_private_key": "5.3 — deux signataires ; M1 signe avec la clé maître",
}

# Read, deliberately not written yet.
AWAITING_WRITER: dict[str, str] = {
    "anchor_price": "8.4 et 8.5 — posé par BUILD et chaque re-centrage ; sans lui P7 ne tire pas",
    "last_skim_at": "8.5 — posé par l'écrémage ; sans lui le créneau paraît toujours ouvert",
}

_KV_CALL = re.compile(r"\.kv_(get|set)\(\s*([^,)]+)")


def _consumer_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in SRC.rglob("*.py")
        if "config" not in path.relative_to(SRC).parts and path.name != "settings.py"
    )


def _leaves(model: type[BaseModel], prefix: str) -> list[tuple[str, str]]:
    """(dotted name, attribute name) for every scalar field, nested models walked."""
    out: list[tuple[str, str]] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.extend(_leaves(annotation, f"{prefix}{name}."))
        else:
            out.append((f"{prefix}{name}", name))
    return out


def _all_leaves() -> list[tuple[str, str]]:
    return _leaves(Config, "") + _leaves(Settings, "env.")


def _read_through_env_indirection(sources: str) -> set[str]:
    """Settings fields consumed via an `env:NAME` config value (`resolve_secret`)."""
    if "resolve_secret(config.alerts" not in sources:
        return set()
    return {
        f"env.{field.default[len('env:') :].lower()}"
        for field in AlertsConfig.model_fields.values()
        if isinstance(field.default, str) and field.default.startswith("env:")
    }


def _unread() -> set[str]:
    sources = _consumer_sources()
    indirect = _read_through_env_indirection(sources)
    return {
        dotted
        for dotted, attr in _all_leaves()
        if dotted not in indirect and not re.search(rf"\.{re.escape(attr)}\b", sources)
    }


def _kv_keys(sources: str, verb: str) -> set[str]:
    return {
        m.group(2).strip().strip("\"'") for m in _KV_CALL.finditer(sources) if m.group(1) == verb
    }


def test_every_setting_has_a_reader_or_names_its_chantier() -> None:
    ghosts = sorted(_unread() - AWAITING_READER.keys())
    assert not ghosts, (
        f"réglages déclarés que rien ne lit : {ghosts}. Branchez un lecteur, retirez le "
        "réglage, ou inscrivez-le dans AWAITING_READER avec le chantier qui le lira."
    )


def test_the_reader_registry_holds_no_stale_entry() -> None:
    declared = {dotted for dotted, _ in _all_leaves()}
    unknown = sorted(AWAITING_READER.keys() - declared)
    assert not unknown, f"AWAITING_READER nomme des réglages qui n'existent pas : {unknown}"

    now_read = sorted(AWAITING_READER.keys() - _unread())
    assert not now_read, (
        f"ces réglages ont maintenant un lecteur : {now_read}. Retirez-les de AWAITING_READER."
    )


def test_every_state_key_read_is_written_or_names_its_chantier() -> None:
    sources = _consumer_sources()
    read, written = _kv_keys(sources, "get"), _kv_keys(sources, "set")

    ghosts = sorted(read - written - AWAITING_WRITER.keys())
    assert not ghosts, (
        f"clés d'état lues que rien n'écrit : {ghosts}. Écrivez-les, ou inscrivez-les dans "
        "AWAITING_WRITER avec le chantier qui les posera."
    )

    stale = sorted(AWAITING_WRITER.keys() & written)
    assert not stale, f"ces clés sont maintenant écrites : {stale}. Retirez-les de AWAITING_WRITER."
    orphans = sorted(AWAITING_WRITER.keys() - read)
    assert not orphans, f"AWAITING_WRITER nomme des clés que rien ne lit : {orphans}"
