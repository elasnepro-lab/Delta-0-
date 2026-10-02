"""Les lectures on-chain du backtest : un RPC minimal, et la conversion date -> bloc.

Deux sources ne s'achètent pas et ne se téléchargent pas en archive : le taux de
conversion wstETH/stETH de Lido, lu à des blocs passés, et le taux d'emprunt
Aave, reconstitué depuis les événements `ReserveDataUpdated`. Les deux passent
par ici.

Ce que ce module tient, et pourquoi :

- **des RPC publics, jamais le quota du serveur.** Le bot a un fournisseur avec
  un quota que le backtest n'a aucune raison de consommer ; ces endpoints-ci
  sont gratuits et vérifiés le 2026-09-16. Arbitrum sert `eth_getLogs` sur tout
  l'historique mais **pas** `eth_call` à un bloc passé (`missing trie node`) ;
  l'archive mainnet, elle, répond (relu : `stEthPerToken` au bloc 11888477) ;
- **on réduit, on ne tronque pas.** Une plage de logs trop dense fait expirer la
  requête ; elle est alors coupée en deux, jusqu'à une taille plancher. Ce qui
  ne peut pas être lu entièrement lève : une plage rendue à moitié deviendrait
  un mois sans emprunt, donc un portage gratuit ;
- **la conversion date -> bloc se fait par interpolation, pas par dichotomie.**
  Les horodatages sont croissants et presque réguliers : partir du rapport
  mesuré converge en quelques sondages là où une dichotomie en demande ~28. Et
  chaque couple (bloc, horodatage) rencontré est gardé sur disque, donc une
  seconde campagne ne le redemande pas. Le rythme des blocs n'est pas supposé :
  il est mesuré entre les deux bornes courantes, ce qui traverse sans dommage la
  fusion d'Ethereum comme la migration Nitro d'Arbitrum.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

DEFAULT_ROOT = Path("data") / "backtest" / "chain"

ENDPOINTS: dict[str, tuple[str, ...]] = {
    "arbitrum": ("https://arb1.arbitrum.io/rpc", "https://arbitrum-one.publicnode.com"),
    "mainnet": ("https://mainnet.gateway.tenderly.co", "https://rpc.mevblocker.io"),
}

ATTEMPTS = 3
BACKOFF_S = (1.0, 2.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_CHUNK = 200_000  # mesuré sur Arbitrum : 1877 événements en 0,83 s
MIN_CHUNK = 2_000  # en deçà, ce n'est plus une plage dense, c'est une panne

WORD_HEX = 64  # un mot de 32 octets, en hexadécimal

# Un sondage qui laisse plus des trois quarts de l'encadrement n'a rien appris :
# le suivant coupe en deux au lieu d'interpoler.
STALL_SHARE = 0.75

_PARTIAL_SUFFIX = ".partial"


class ChainError(Exception):
    """La chaîne n'a pas rendu quelque chose d'utilisable, et on ne devine pas."""


def word(data: str, index: int) -> int:
    """Le mot de 32 octets numéro `index` du champ `data` d'un log."""
    raw = data[2:] if data.startswith("0x") else data
    start = index * WORD_HEX
    chunk = raw[start : start + WORD_HEX]
    if len(chunk) < WORD_HEX:
        raise ChainError(
            f"champ data trop court : {len(raw) // WORD_HEX} mots, mot {index} demandé"
        )
    return int(chunk, 16)


def topic_for_address(address: str) -> str:
    """Une adresse en topic indexé : 12 octets de zéros, puis l'adresse."""
    return "0x" + "0" * 24 + address[2:].lower()


class Chain:
    """Un accès en lecture seule à une chaîne, par RPC publics successifs."""

    def __init__(
        self,
        name: str,
        client: httpx.Client,
        *,
        endpoints: Sequence[str] | None = None,
        root: Path = DEFAULT_ROOT,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self.client = client
        self.endpoints = tuple(endpoints) if endpoints is not None else ENDPOINTS[name]
        self.root = root
        self.pause = pause
        self.calls = 0
        self._timestamps: dict[int, int] = self._load_timestamps()
        self._unsaved = 0

    # --- Le transport ---------------------------------------------------------

    def rpc(self, method: str, params: list[Any]) -> Any:
        """Une méthode JSON-RPC, retentée puis basculée d'un endpoint à l'autre."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = "aucun endpoint configuré"
        for url in self.endpoints:
            for attempt in range(ATTEMPTS):
                try:
                    self.calls += 1
                    response = self.client.post(url, json=payload)
                    if response.status_code in RETRY_STATUS:
                        last = f"{url} a répondu {response.status_code}"
                    else:
                        response.raise_for_status()
                        body = response.json()
                        if "error" in body:
                            # Un refus du nœud (plage expirée, bloc élagué) : un
                            # autre endpoint peut très bien répondre.
                            last = f"{url} a refusé {method} : {body['error']}"
                        else:
                            return body["result"]
                except httpx.HTTPError as e:
                    last = f"{url} : {e}"
                if attempt + 1 < ATTEMPTS:
                    self.pause(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        raise ChainError(f"{method} sans réponse utilisable — {last}")

    def block_number(self) -> int:
        return int(self.rpc("eth_blockNumber", []), 16)

    def call_at(self, to: str, data: str, block: int) -> int:
        """Un appel `view` rendant un seul entier, à un bloc passé (nœud d'archive)."""
        return int(self.call_raw(to, data, block), 16)

    def call_raw(self, to: str, data: str, block: int) -> str:
        """Le retour brut d'un appel `view` : plusieurs mots se décodent avec `word`."""
        result = self.rpc("eth_call", [{"to": to, "data": data}, hex(block)])
        if not isinstance(result, str) or not result.startswith("0x") or not result[2:]:
            # Une réponse vide n'est pas un zéro : c'est une adresse sans code à
            # ce bloc, ou un appel rejeté. La confondre avec une valeur ferait
            # entrer un 0 dans une série de prix ou de ratios.
            raise ChainError(
                f"eth_call a rendu {str(result)[:80]!r} au bloc {block} :"
                " pas de code à cette adresse à ce bloc, ou appel rejeté"
            )
        return result

    # --- Le temps -------------------------------------------------------------

    def _timestamps_file(self) -> Path:
        return self.root / f"{self.name}-blocks.json"

    def _load_timestamps(self) -> dict[int, int]:
        path = self._timestamps_file()
        if not path.is_file():
            return {}
        try:
            stored = json.loads(path.read_text(encoding="ascii"))
            return {int(block): int(ts) for block, ts in stored.items()}
        except (ValueError, TypeError, OSError):
            return {}  # un cache illisible se refait, il ne se devine pas

    def flush(self) -> None:
        """Garder les horodatages rencontrés : une campagne coupée ne les redemande pas.

        Sans effet si rien n'a été appris depuis la dernière fois : une campagne
        horaire appelle `block_at` des centaines de fois par mois, et réécrire le
        fichier à chaque appel coûterait plus que les lectures elles-mêmes.
        """
        if not self._unsaved:
            return
        path = self._timestamps_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + _PARTIAL_SUFFIX)
        partial.write_text(
            json.dumps({str(block): ts for block, ts in sorted(self._timestamps.items())}),
            encoding="ascii",
        )
        os.replace(partial, path)
        self._unsaved = 0

    def timestamp_of(self, block: int) -> int:
        cached = self._timestamps.get(block)
        if cached is not None:
            return cached
        header = self.rpc("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(header, dict) or "timestamp" not in header:
            raise ChainError(f"bloc {block} sans en-tête exploitable")
        stamp = int(header["timestamp"], 16)
        self._timestamps[block] = stamp
        self._unsaved += 1
        return stamp

    def block_at(self, target_ts: int, *, head: int | None = None) -> int:
        """Le dernier bloc dont l'horodatage ne dépasse pas `target_ts`.

        Interpolation tant qu'elle avance, dichotomie dès qu'elle patine. La
        deuxième moitié de la règle n'est pas une précaution théorique : sur
        Arbitrum, dater deux instants a coûté **602 appels** avec la seule
        interpolation. La migration Nitro a changé la cadence d'un facteur
        proche de cent, si bien qu'un rapport mesuré sur tout l'intervalle place
        le sondage presque toujours du même côté et ne resserre plus rien. Une
        dichotomie forcée après un sondage stérile borne le pire cas au
        logarithme, sans rien coûter quand la chaîne est régulière.
        """
        low, high = 0, self.block_number() if head is None else head
        low_ts, high_ts = self.timestamp_of(low), self.timestamp_of(high)
        if target_ts < low_ts:
            raise ChainError(f"{target_ts} est avant le premier bloc de {self.name} ({low_ts})")
        if target_ts >= high_ts:
            self.flush()
            return high

        stalled = False
        while high - low > 1:
            before = high - low
            if stalled:
                guess = low + before // 2
            else:
                span = high_ts - low_ts
                share = (target_ts - low_ts) / span if span > 0 else 0.5
                guess = low + int(before * share)
            guess = min(max(guess, low + 1), high - 1)  # toujours avancer

            guess_ts = self.timestamp_of(guess)
            if guess_ts <= target_ts:
                low, low_ts = guess, guess_ts
            else:
                high, high_ts = guess, guess_ts
            stalled = (high - low) > before * STALL_SHARE

        self.flush()
        return low

    # --- Les événements -------------------------------------------------------

    def logs_range(
        self,
        address: str,
        topics: Sequence[str | None],
        from_block: int,
        to_block: int,
        *,
        chunk: int = DEFAULT_CHUNK,
    ) -> list[dict[str, Any]]:
        """Tous les logs de la plage, par morceaux, coupés en deux quand c'est trop dense.

        Rend la plage entière ou lève. Une plage rendue à moitié deviendrait un
        mois sans emprunt, c'est-à-dire un portage gratuit.
        """
        found: list[dict[str, Any]] = []
        start = from_block
        while start <= to_block:
            stop = min(start + chunk - 1, to_block)
            found.extend(self._logs_chunk(address, topics, start, stop, chunk))
            start = stop + 1
        return found

    def _logs_chunk(
        self,
        address: str,
        topics: Sequence[str | None],
        from_block: int,
        to_block: int,
        chunk: int,
    ) -> list[dict[str, Any]]:
        try:
            result = self.rpc(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "address": address,
                        "topics": list(topics),
                    }
                ],
            )
        except ChainError:
            if chunk <= MIN_CHUNK or from_block == to_block:
                raise
            middle = (from_block + to_block) // 2
            half = max(chunk // 2, MIN_CHUNK)
            return self._logs_chunk(address, topics, from_block, middle, half) + self._logs_chunk(
                address, topics, middle + 1, to_block, half
            )
        if not isinstance(result, list):
            raise ChainError(f"eth_getLogs a rendu {str(result)[:80]!r}")
        return result
