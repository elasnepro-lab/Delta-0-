"""Bridge executor — Arbitrum ↔ Hyperliquid round trip (chemins P5/P6).

M1-B2 goal: measure the real end-to-end latency of moving USDC in and out
of the HL account. README §9.3:
- Arbitrum → HL: ERC20 transfer to Bridge2 contract, credited after finality
  (~1-3 min).
- HL → Arbitrum: withdraw via HL API (signed), then validator window
  (~4-8 min), fee of 1 USDC per withdrawal.

Design:
- `bridge_out(amount)`: signs and sends the ERC20 transfer to Bridge2, waits
  for Arbitrum receipt, returns immediately. Polling for HL credit is a
  separate call (`wait_for_hl_credit`) so tests can stub it independently.
- `bridge_in(amount)`: uses the HL SDK `withdraw_from_bridge`, then
  `wait_for_arbitrum_credit` polls the Arbitrum USDC balance.
- `round_trip(amount)`: composes the four steps and records the two
  distinct latencies `path.p5_bridge_up` and `path.p6_bridge_down`.

Safety: op_kinds `bridge_out` / `bridge_in` already in the allowlist. Amount
capped by the guard's `max_op_usd`. HL's own minimums (5 USDC deposit,
2 USDC withdrawal) enforced client-side to fail-fast rather than let the
tx revert.

Dry-run: neither leg touches the network in dry_run mode. The intent is
still journaled so the tracer flow is exercised.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from eth_typing import ChecksumAddress
from web3 import AsyncWeb3

from delta0.config import Config
from delta0.latency import elapsed_ms, now_perf
from delta0.logging import get_logger
from delta0.safety import MicroOpsGuard
from delta0.state import StateStore, deterministic_id

log = get_logger(__name__)


# HL client-side minimums (README §9.1).
MIN_DEPOSIT_USDC = 5.0
MIN_WITHDRAW_USDC = 2.0

# ERC-20 transfer ABI — minimal.
_ERC20_TRANSFER_ABI: list[dict[str, Any]] = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]


@dataclass(frozen=True, slots=True)
class BridgeLegResult:
    """Outcome of one direction of a bridge move."""

    intent_id: str
    status: Literal["confirmed", "failed", "dry_run"]
    tx_hash: str | None
    duration_ms: float


@dataclass(frozen=True, slots=True)
class RoundTripResult:
    up: BridgeLegResult
    up_credit_wait_ms: float
    down: BridgeLegResult
    down_credit_wait_ms: float


class BridgeExecutor:
    """Micro-op executor for Arbitrum ↔ HL bridge traversals."""

    def __init__(
        self,
        *,
        web3: AsyncWeb3,  # type: ignore[type-arg]
        config: Config,
        store: StateStore,
        guard: MicroOpsGuard,
        master_address: str,
        chain_id: int,
        hl_exchange_factory: Any,
        hl_info: Any,
        private_key: str | None = None,
    ) -> None:
        self._w3 = web3
        self._config = config
        self._store = store
        self._guard = guard
        self._master: ChecksumAddress = AsyncWeb3.to_checksum_address(master_address)
        self._chain_id = chain_id
        self._make_hl_exchange = hl_exchange_factory
        self._hl_info = hl_info
        self._private_key = private_key
        self._usdc: ChecksumAddress = AsyncWeb3.to_checksum_address(
            config.venues.usdc_address,
        )
        self._bridge2: ChecksumAddress = AsyncWeb3.to_checksum_address(
            config.venues.hl_bridge2,
        )
        self._usdc_contract = web3.eth.contract(
            address=self._usdc,
            abi=_ERC20_TRANSFER_ABI,
        )

    # --- Public micro-ops -----------------------------------------------------

    async def bridge_out(self, amount_usdc: float) -> BridgeLegResult:
        """Arbitrum → HL. Sends USDC to Bridge2, waits for Arbitrum receipt."""
        if amount_usdc < MIN_DEPOSIT_USDC:
            raise ValueError(
                f"HL dépôt minimum est {MIN_DEPOSIT_USDC} USDC, {amount_usdc} refusé",
            )
        self._guard.check("bridge_out", notional_usd=amount_usdc)
        return await self._journal_and_send_erc20_transfer(
            op_kind="bridge_out",
            destination=self._bridge2,
            amount_usdc=amount_usdc,
        )

    async def bridge_in(self, amount_usdc: float) -> BridgeLegResult:
        """HL → Arbitrum via HL SDK `withdraw_from_bridge`."""
        if amount_usdc < MIN_WITHDRAW_USDC:
            raise ValueError(
                f"HL retrait minimum est {MIN_WITHDRAW_USDC} USDC, {amount_usdc} refusé",
            )
        self._guard.check("bridge_in", notional_usd=amount_usdc)

        intent_id = deterministic_id(
            "bridge_in",
            f"{amount_usdc:.6f}",
            self._master,
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        await self._insert_pending_intent(
            intent_id,
            "bridge_in",
            {"amount_usdc": amount_usdc, "destination": self._master},
        )
        start = now_perf()

        if self._config.tracer.dry_run:
            log.info(
                "bridge_in_dry_run",
                message=f"bridge_in: dry-run {amount_usdc} USDC",
                amount=amount_usdc,
            )
            duration_ms = elapsed_ms(start)
            await self._store.record_latency("path.bridge_in_submit", duration_ms)
            await self._mark_intent_status(intent_id, "confirmed", None)
            return BridgeLegResult(
                intent_id=intent_id,
                status="dry_run",
                tx_hash=None,
                duration_ms=duration_ms,
            )

        try:
            exchange = self._make_hl_exchange()
            await asyncio.to_thread(
                exchange.withdraw_from_bridge,
                amount_usdc,
                self._master,
            )
        except Exception:
            await self._mark_intent_status(intent_id, "failed", None)
            log.exception(
                "bridge_in_failed",
                message="bridge_in: échec du retrait HL",
                intent_id=intent_id,
            )
            raise

        duration_ms = elapsed_ms(start)
        await self._store.record_latency("path.bridge_in_submit", duration_ms)
        await self._mark_intent_status(intent_id, "confirmed", None)
        log.info(
            "bridge_in_submitted",
            message=f"bridge_in soumis en {duration_ms:.1f} ms",
            amount=amount_usdc,
        )
        return BridgeLegResult(
            intent_id=intent_id,
            status="confirmed",
            tx_hash=None,
            duration_ms=duration_ms,
        )

    async def wait_for_hl_credit(
        self,
        amount_usdc: float,
        *,
        timeout_s: float = 900.0,
        poll_interval_s: float = 10.0,
    ) -> float:
        """Poll HL user_state until USDC balance rises by ~`amount_usdc`.

        Returns wait duration in milliseconds. Raises TimeoutError on timeout.
        """
        start_balance = await self._get_hl_usdc_balance()
        target = start_balance + amount_usdc * 0.99  # tolerate a small rounding
        start = now_perf()
        deadline = start + timeout_s
        while now_perf() < deadline:
            current = await self._get_hl_usdc_balance()
            if current >= target:
                waited_ms = elapsed_ms(start)
                await self._store.record_latency("path.p5_bridge_up", waited_ms)
                log.info(
                    "bridge_up_credited",
                    message=f"USDC crédité sur HL après {waited_ms / 1000:.1f} s",
                    waited_ms=waited_ms,
                    amount=amount_usdc,
                )
                return waited_ms
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(
            f"Aucun crédit HL détecté après {timeout_s} s pour {amount_usdc} USDC",
        )

    async def wait_for_arbitrum_credit(
        self,
        amount_usdc: float,
        *,
        timeout_s: float = 900.0,
        poll_interval_s: float = 15.0,
    ) -> float:
        """Poll Arbitrum USDC balance until it rises by ~`amount_usdc - fee`."""
        start_balance = await self._get_arb_usdc_balance()
        # HL charges ~1 USDC on withdrawal (README §9.1).
        target = start_balance + max(0.0, amount_usdc - 1.5)
        start = now_perf()
        deadline = start + timeout_s
        while now_perf() < deadline:
            current = await self._get_arb_usdc_balance()
            if current >= target:
                waited_ms = elapsed_ms(start)
                await self._store.record_latency("path.p6_bridge_down", waited_ms)
                log.info(
                    "bridge_down_credited",
                    message=f"USDC crédité sur Arbitrum après {waited_ms / 1000:.1f} s",
                    waited_ms=waited_ms,
                    amount=amount_usdc,
                )
                return waited_ms
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(
            f"Aucun crédit Arbitrum détecté après {timeout_s} s pour {amount_usdc} USDC",
        )

    async def round_trip(self, amount_usdc: float) -> RoundTripResult:
        """Aller-retour complet mesuré : bridge_out + wait + bridge_in + wait.

        Enregistre p5_bridge_up et p6_bridge_down. En dry_run, les deux waits
        sont court-circuités (ils reposeraient sur des balances qui ne bougent
        pas).
        """
        up = await self.bridge_out(amount_usdc)
        if self._config.tracer.dry_run:
            up_wait = 0.0
        else:
            up_wait = await self.wait_for_hl_credit(amount_usdc)

        down = await self.bridge_in(amount_usdc)
        if self._config.tracer.dry_run:
            down_wait = 0.0
        else:
            down_wait = await self.wait_for_arbitrum_credit(amount_usdc)

        return RoundTripResult(
            up=up,
            up_credit_wait_ms=up_wait,
            down=down,
            down_credit_wait_ms=down_wait,
        )

    # --- Internals ------------------------------------------------------------

    async def _journal_and_send_erc20_transfer(
        self,
        *,
        op_kind: str,
        destination: ChecksumAddress,
        amount_usdc: float,
    ) -> BridgeLegResult:
        decimals: int = await self._usdc_contract.functions.decimals().call()
        raw_amount = int(amount_usdc * (10**decimals))

        intent_id = deterministic_id(
            op_kind,
            f"{amount_usdc:.6f}",
            destination,
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        await self._insert_pending_intent(
            intent_id,
            op_kind,
            {
                "amount_usdc": amount_usdc,
                "destination": destination,
                "raw_amount": raw_amount,
            },
        )
        start = now_perf()

        if self._config.tracer.dry_run:
            log.info(
                f"{op_kind}_dry_run",
                message=f"{op_kind}: dry-run {amount_usdc} USDC vers {destination}",
                amount=amount_usdc,
            )
            duration_ms = elapsed_ms(start)
            await self._store.record_latency(f"path.{op_kind}_submit", duration_ms)
            await self._mark_intent_status(intent_id, "confirmed", None)
            return BridgeLegResult(
                intent_id=intent_id,
                status="dry_run",
                tx_hash=None,
                duration_ms=duration_ms,
            )

        try:
            call = self._usdc_contract.functions.transfer(destination, raw_amount)
            tx = await call.build_transaction(
                {
                    "from": self._master,
                    "nonce": await self._w3.eth.get_transaction_count(self._master),
                    "chainId": self._chain_id,
                },
            )
            signed = self._w3.eth.account.sign_transaction(tx, private_key=self._pkey())
            tx_hash = await self._w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash)
        except Exception:
            await self._mark_intent_status(intent_id, "failed", None)
            log.exception(
                f"{op_kind}_failed",
                message=f"{op_kind}: envoi ou attente de reçu en échec",
                intent_id=intent_id,
            )
            raise

        duration_ms = elapsed_ms(start)
        await self._store.record_latency(f"path.{op_kind}_submit", duration_ms)
        status_int = int(receipt.get("status", 0))
        if status_int != 1:
            await self._mark_intent_status(intent_id, "failed", [tx_hash.hex()])
            log.error(
                f"{op_kind}_reverted",
                message=f"{op_kind}: transaction reverted",
                intent_id=intent_id,
            )
            return BridgeLegResult(
                intent_id=intent_id,
                status="failed",
                tx_hash=tx_hash.hex(),
                duration_ms=duration_ms,
            )

        await self._mark_intent_status(intent_id, "confirmed", [tx_hash.hex()])
        log.info(
            f"{op_kind}_confirmed",
            message=f"{op_kind} confirmé en {duration_ms:.1f} ms",
            duration_ms=duration_ms,
        )
        return BridgeLegResult(
            intent_id=intent_id,
            status="confirmed",
            tx_hash=tx_hash.hex(),
            duration_ms=duration_ms,
        )

    async def _get_arb_usdc_balance(self) -> float:
        decimals: int = int(await self._usdc_contract.functions.decimals().call())
        raw: int = int(await self._usdc_contract.functions.balanceOf(self._master).call())
        result: float = raw / (10**decimals)
        return result

    async def _get_hl_usdc_balance(self) -> float:
        state = await asyncio.to_thread(self._hl_info.user_state, self._master)
        if not isinstance(state, dict):
            return 0.0
        margin = state.get("marginSummary")
        if not isinstance(margin, dict):
            return 0.0
        raw = margin.get("accountValue", "0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _pkey(self) -> str:
        if not self._private_key:
            raise NotImplementedError(
                "private key not provided — pass private_key= to constructor "
                "(the CLI wires it from .env via --live-micro-ops)",
            )
        return self._private_key

    async def _insert_pending_intent(
        self,
        intent_id: str,
        op_kind: str,
        params: dict[str, object],
    ) -> None:
        assert self._store._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._store._conn.execute(
            """
            INSERT OR IGNORE INTO intents
                (id, created_at, action, priority, params_json, reason, status, updated_at)
            VALUES (?, ?, ?, 0, ?, 'micro-op M1-B2 bridge', 'pending', ?)
            """,
            (intent_id, now, op_kind, json.dumps(params, sort_keys=True, default=str), now),
        )
        await self._store._conn.commit()

    async def _mark_intent_status(
        self,
        intent_id: str,
        status: Literal["sent", "confirmed", "failed"],
        tx_hashes: list[str] | None,
    ) -> None:
        assert self._store._conn is not None
        await self._store._conn.execute(
            """
            UPDATE intents
               SET status = ?, tx_hashes = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                status,
                json.dumps(tx_hashes) if tx_hashes else None,
                datetime.now(UTC).isoformat(),
                intent_id,
            ),
        )
        await self._store._conn.commit()
