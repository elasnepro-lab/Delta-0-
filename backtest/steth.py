"""Le prix de MARCHÉ du stETH en ETH — celui de la sortie, pas celui du danger.

À ne pas confondre avec le taux de conversion de Lido (`backtest/lido.py`), qui
seul entre dans le facteur de santé depuis le 2023-06-26. Le décrochage de juin
2022 n'a jamais pu liquider le montage : il coûte à la **sortie**, quand il faut
vendre le wstETH contre de l'ETH.

Deux sources, qui ne répondent pas à la même question :

- **Chainlink stETH/ETH** dit ce que l'oracle affichait. Lu sur le proxy, round
  par round, sans nœud d'archive : un round coûte 0,03 s. Son rythme est d'un
  point par jour (battement de 24 h plus les écarts), mesuré le 2026-09-16 :
  round 300 le 2022-06-14, round 305 le 2022-06-19. **Le creux intrajournalier
  y est donc invisible**, et c'est une limite du flux, pas de ce module ;
- **Curve `get_dy`** dit ce qu'un vendeur aurait réellement obtenu pour un stETH,
  glissement compris, à un bloc donné. C'est la grandeur qui compte pour une
  sortie, et la seule qui montre le creux du 15 juin 2022 (0,93815 contre 0,935
  au plus bas Chainlink).

Les rounds se composent `(phase << 64) | numéro`. Vérifié le 2026-09-16 : phase
1 du round 1 (2021-08-25) au round 1099 (2024-09-06), phase 2 depuis, décimales
18, et le plus bas annoncé par l'inventaire retrouvé au round 304 de la phase 1
— 0,93502 le 2022-06-18 09:09 UTC.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from backtest.chain import Chain, word

PROXY = "0x86392dC19c0b719886221c78AB11eb8Cf5c52812"  # flux Chainlink stETH/ETH
CURVE_POOL = "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022"

DECIMALS_SELECTOR = "0x313ce567"  # decimals()
PHASE_ID_SELECTOR = "0x58303b10"  # phaseId()
GET_ROUND_SELECTOR = "0x9a6fc8f5"  # getRoundData(uint80)
GET_DY_SELECTOR = "0x5e0d443f"  # get_dy(int128,int128,uint256)

EXPECTED_DECIMALS = 18
WEI = 10**18
ROUND_MASK = (1 << 64) - 1

Progress = Callable[[str, int], None]  # (état, numéro de round ou instant)

_PARTIAL_SUFFIX = ".partial"


class StethError(Exception):
    """Ce que la chaîne a rendu ne peut pas être un prix de marché du stETH."""


@dataclass(frozen=True, slots=True)
class Round:
    """Un point du flux Chainlink, tel qu'il a été publié."""

    phase: int
    number: int
    ts: int  # updatedAt
    raw: int  # 18 décimales

    @property
    def ratio(self) -> float:
        return self.raw / WEI


@dataclass(frozen=True, slots=True)
class Sample:
    """Ce qu'un vendeur d'un stETH aurait obtenu sur Curve, à un bloc donné."""

    ts: int
    block: int
    raw: int

    @property
    def ratio(self) -> float:
        return self.raw / WEI


def compose(phase: int, number: int) -> int:
    return (phase << 64) | number


def rounds_file(root: Path, phase: int) -> Path:
    return root / "steth" / f"chainlink-phase{phase}.json"


def curve_file(root: Path) -> Path:
    return root / "steth" / "curve.json"


def _write(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + _PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document), encoding="ascii")
    os.replace(partial, path)
    return path


def check_decimals(chain: Chain, block: int) -> None:
    """Une décimale qui change réécrirait toute la série sans prévenir."""
    decimals = chain.call_at(PROXY, DECIMALS_SELECTOR, block)
    if decimals != EXPECTED_DECIMALS:
        raise StethError(f"le flux annonce {decimals} décimales, {EXPECTED_DECIMALS} attendues")


def read_round(chain: Chain, phase: int, number: int, block: int) -> Round | None:
    """Un round du flux, ou None s'il n'a pas encore été publié.

    Les numéros sont contigus par agrégateur : le premier round vide marque la
    fin de la phase, il ne se saute pas.
    """
    data = GET_ROUND_SELECTOR + f"{compose(phase, number):064x}"
    raw = chain.call_raw(PROXY, data, block)
    updated_at = word(raw, 3)
    if updated_at == 0:
        return None
    answer = word(raw, 1)
    if answer <= 0:
        raise StethError(f"round {phase}/{number} : réponse {answer}, impossible pour un prix")
    return Round(phase=phase, number=number, ts=updated_at, raw=answer)


def load_rounds(root: Path, phase: int) -> list[Round]:
    path = rounds_file(root, phase)
    if not path.is_file():
        return []
    try:
        stored = json.loads(path.read_text(encoding="ascii"))
        return [
            Round(phase=phase, number=int(row[0]), ts=int(row[1]), raw=int(row[2]))
            for row in stored["rounds"]
        ]
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        return []  # un cache illisible se refait, il ne se devine pas


def save_rounds(root: Path, phase: int, rounds: Sequence[Round]) -> Path:
    document = {
        "feed": "stETH/ETH",
        "phase": phase,
        "rounds": [[r.number, r.ts, r.raw] for r in rounds],
    }
    return _write(rounds_file(root, phase), document)


def ensure_phase(
    chain: Chain,
    root: Path,
    phase: int,
    *,
    head: int | None = None,
    progress: Progress | None = None,
) -> list[Round]:
    """Tous les rounds publiés d'une phase, repris là où le cache s'était arrêté."""
    block = chain.block_number() if head is None else head
    check_decimals(chain, block)

    rounds = load_rounds(root, phase)
    number = rounds[-1].number + 1 if rounds else 1
    fresh = 0
    while True:
        taken = read_round(chain, phase, number, block)
        if taken is None:
            break
        if rounds and taken.ts < rounds[-1].ts:
            raise StethError(f"round {phase}/{number} antérieur au précédent : {taken.ts}")
        rounds.append(taken)
        fresh += 1
        if progress is not None:
            progress("fetched", number)
        number += 1
    if fresh:
        save_rounds(root, phase, rounds)
    return rounds


def all_rounds(root: Path, phases: Sequence[int] = (1, 2)) -> list[Round]:
    """Les phases mises bout à bout, dans l'ordre du temps."""
    joined: list[Round] = []
    for phase in phases:
        joined.extend(load_rounds(root, phase))
    return sorted(joined, key=lambda r: r.ts)


def ratio_at(rounds: Sequence[Round], ts: int) -> float:
    """Ce que l'oracle affichait à cet instant. Jamais extrapolé vers l'arrière.

    Entre deux rounds la valeur ne bouge pas : c'est bien ce que l'oracle
    affichait, et c'est pourquoi un creux intrajournalier n'y figure pas.
    """
    seen: Round | None = None
    for entry in rounds:
        if entry.ts > ts:
            break
        seen = entry
    if seen is None:
        raise StethError(f"aucun round à {ts} ou avant : le flux ne commence pas si tôt")
    return seen.ratio


def lowest(rounds: Sequence[Round], from_ts: int, to_ts: int) -> Round:
    """Le round le plus bas de la fenêtre, tel qu'il a été publié."""
    window = [entry for entry in rounds if from_ts <= entry.ts <= to_ts]
    if not window:
        raise StethError(f"aucun round entre {from_ts} et {to_ts}")
    return min(window, key=lambda entry: entry.raw)


# --- Ce qu'un vendeur aurait obtenu -------------------------------------------


def execution_raw(chain: Chain, block: int) -> int:
    """`get_dy(stETH -> ETH, 1 stETH)` à un bloc passé : glissement compris."""
    data = GET_DY_SELECTOR + f"{1:064x}{0:064x}{WEI:064x}"
    got = chain.call_at(CURVE_POOL, data, block)
    if got <= 0:
        raise StethError(f"Curve rend {got} au bloc {block}")
    return got


def load_samples(root: Path) -> dict[int, Sample]:
    path = curve_file(root)
    if not path.is_file():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="ascii"))
        return {
            int(ts): Sample(ts=int(ts), block=int(row[0]), raw=int(row[1]))
            for ts, row in stored.items()
        }
    except (ValueError, TypeError, IndexError, OSError):
        return {}


def save_samples(root: Path, samples: dict[int, Sample]) -> Path:
    document = {str(ts): [s.block, s.raw] for ts, s in sorted(samples.items())}
    return _write(curve_file(root), document)


def ensure_samples(
    chain: Chain,
    root: Path,
    first_ts: int,
    last_ts: int,
    step_s: int,
    *,
    progress: Progress | None = None,
) -> list[Sample]:
    """Le prix d'exécution Curve, échantillonné sur une fenêtre, gardé sur disque.

    Réservé aux épisodes qui le méritent : c'est un appel d'archive par point.
    """
    if step_s <= 0:
        raise StethError(f"pas d'échantillonnage impossible : {step_s} s")
    samples = load_samples(root)
    head: int | None = None  # demandée au premier manque, pas avant
    fresh = 0
    for ts in range(first_ts, last_ts + 1, step_s):
        if ts in samples:
            if progress is not None:
                progress("cached", ts)
            continue
        if head is None:
            head = chain.block_number()
        block = chain.block_at(ts, head=head)
        samples[ts] = Sample(ts=ts, block=block, raw=execution_raw(chain, block))
        fresh += 1
        if progress is not None:
            progress("fetched", ts)
    if fresh:
        save_samples(root, samples)
        chain.flush()
    return [samples[ts] for ts in range(first_ts, last_ts + 1, step_s) if ts in samples]
