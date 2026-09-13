"""M1-B2 Aave micro-op executor.

Executes tiny Aave v3 operations (supply / borrow / repay / withdraw) to
measure the local Aave latency of chemins P3 / P4 (README §7). Not a general
purpose executor — the actions available here are the four Aave verbs plus
ERC-20 approve. HL orders and the bridge land in follow-up modules.

Every operation:
1. Passes through `MicroOpsGuard.check(...)` BEFORE any network activity.
2. Writes a `pending` intent to SQLite BEFORE sending.
3. Sends the tx (or skips if `config.tracer.dry_run` is True).
4. Waits for the receipt.
5. Updates the intent to `confirmed` (or `failed`), records latency in ms.

If any step throws AFTER the pending intent is written, the intent stays
`sent` and the boot reconciliation will surface it on next start (README §13).

Amount API: every method takes an amount in the token's native units
(e.g. 10.0 USDC = 10 USDC, not 10_000_000 raw). Conversions to raw happen
inside the executor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from eth_typing import ChecksumAddress
from web3 import AsyncWeb3

from delta0 import failure
from delta0.config import Config
from delta0.gas import with_gas_margin
from delta0.latency import elapsed_ms, measurement_path, now_perf
from delta0.logging import get_logger
from delta0.safety import InsufficientBalance, MicroOpsGuard
from delta0.state import StateStore, deterministic_id
from delta0.units import Rounding, to_raw
from delta0.venues.aave import AaveTokenBalances

log = get_logger(__name__)


AaveOpKind = Literal[
    "aave_approve",
    "aave_supply",
    "aave_borrow",
    "aave_repay",
    "aave_withdraw",
]


# Aave v3 Pool + ERC20 minimal ABI (mutations only — reads live in venues/aave.py).
_POOL_MUT_ABI: list[dict[str, Any]] = [
    {
        "name": "supply",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "name": "borrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "repay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


_ERC20_MUT_ABI: list[dict[str, Any]] = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
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


# Aave v3: variable rate mode = 2 (stable = 1 — deprecated on most markets).
_VARIABLE_RATE_MODE = 2
_REFERRAL_CODE = 0


class BalanceReader(Protocol):
    """The one read the funding guard needs. Structural, so a test can fake it."""

    async def read_token_balances(self, asset: str) -> AaveTokenBalances: ...


@dataclass(frozen=True, slots=True)
class OpResult:
    """Outcome of one micro-op."""

    intent_id: str
    tx_hash: str | None  # None in dry_run
    status: Literal["confirmed", "failed", "dry_run"]
    duration_ms: float
    gas_used: int | None


# The one rounding each operation may apply when its amount becomes native units
# (chantier 6.5). What spends or creates debt never exceeds the amount asked;
# what authorizes or pays back never falls short of it. Aave caps a repay at the
# outstanding debt, so rounding a repay up cannot overpay.
_ROUNDING: dict[str, Rounding] = {
    "aave_approve": Rounding.UP,
    "aave_supply": Rounding.DOWN,
    "aave_borrow": Rounding.DOWN,
    "aave_repay": Rounding.UP,
    "aave_withdraw": Rounding.DOWN,
}


class AaveTraceExecutor:
    """Small Aave writer used by the M1-B2 latency-measurement tracer.

    Not thread-safe. Callers must serialize their own use (README §11 I7:
    "une seule opération d'exécution en cours à tout instant").
    """

    def __init__(
        self,
        *,
        web3: AsyncWeb3,  # type: ignore[type-arg]
        config: Config,
        store: StateStore,
        guard: MicroOpsGuard,
        master_address: str,
        chain_id: int,
        private_key: str | None = None,
        balances: BalanceReader | None = None,
    ) -> None:
        self._w3 = web3
        self._config = config
        self._store = store
        self._guard = guard
        self._master: ChecksumAddress = AsyncWeb3.to_checksum_address(master_address)
        self._chain_id = chain_id
        self._pool_address: ChecksumAddress = AsyncWeb3.to_checksum_address(
            config.venues.aave_pool,
        )
        self._pool = web3.eth.contract(address=self._pool_address, abi=_POOL_MUT_ABI)
        # Held only when the CLI explicitly wired --live-micro-ops. Never
        # logged (structlog + our own code never format `_private_key`).
        self._private_key = private_key
        # Without a reader the funding guard cannot run. It is optional rather
        # than required so that a caller wiring an executor for something else
        # is not forced to build one, and its absence is logged at the first
        # operation that would have been checked — never silently skipped.
        self._balances = balances

    # --- Public micro-op API --------------------------------------------------

    async def approve(self, asset: str, amount_native: float) -> OpResult:
        """Approve the Aave Pool to pull `amount_native` of `asset`."""
        self._guard.check("aave_approve", self._estimate_notional(asset, amount_native))
        return await self._erc20_write(
            op_kind="aave_approve",
            asset=asset,
            amount_native=amount_native,
            build_call=lambda contract, raw_amount: contract.functions.approve(
                self._pool_address,
                raw_amount,
            ),
        )

    async def supply(self, asset: str, amount_native: float) -> OpResult:
        self._guard.check("aave_supply", self._estimate_notional(asset, amount_native))
        await self._refuse_if_underfunded("aave_supply", asset, needed=amount_native)
        return await self._pool_write(
            op_kind="aave_supply",
            asset=asset,
            amount_native=amount_native,
            build_call=lambda raw_amount: self._pool.functions.supply(
                AsyncWeb3.to_checksum_address(asset),
                raw_amount,
                self._master,
                _REFERRAL_CODE,
            ),
        )

    async def borrow(self, asset: str, amount_native: float) -> OpResult:
        self._guard.check("aave_borrow", self._estimate_notional(asset, amount_native))
        return await self._pool_write(
            op_kind="aave_borrow",
            asset=asset,
            amount_native=amount_native,
            build_call=lambda raw_amount: self._pool.functions.borrow(
                AsyncWeb3.to_checksum_address(asset),
                raw_amount,
                _VARIABLE_RATE_MODE,
                _REFERRAL_CODE,
                self._master,
            ),
        )

    async def repay(self, asset: str, amount_native: float) -> OpResult:
        self._guard.check("aave_repay", self._estimate_notional(asset, amount_native))
        return await self._pool_write(
            op_kind="aave_repay",
            asset=asset,
            amount_native=amount_native,
            build_call=lambda raw_amount: self._pool.functions.repay(
                AsyncWeb3.to_checksum_address(asset),
                raw_amount,
                _VARIABLE_RATE_MODE,
                self._master,
            ),
        )

    async def repay_all(self, asset: str) -> OpResult:
        """Repay the FULL outstanding debt via MAX_UINT256 sentinel.

        Required to end a round trip cleanly — a partial repay leaves accrued
        interest and blocks a subsequent full withdraw (see memory/aave_findings.md).
        The safety guard is fed the *approve headroom* as a notional (a couple
        of USD above the borrow amount), which is what actually leaves the
        wallet in the worst case.
        """
        max_uint = 2**256 - 1
        op_kind: AaveOpKind = "aave_repay"
        self._guard.check(op_kind, notional_usd=self._estimate_notional(asset, 2.0))
        # MAX_UINT256 does not mean "whatever I have": Aave pulls the full
        # outstanding debt, so that is the amount the wallet must hold. It is
        # the exact shape of the 8 September failure, where a wallet at 2,97
        # was asked for 35,01.
        await self._refuse_if_underfunded(op_kind, asset, needed=None)

        call = self._pool.functions.repay(
            AsyncWeb3.to_checksum_address(asset),
            max_uint,
            _VARIABLE_RATE_MODE,
            self._master,
        )
        return await self._journal_and_send(
            op_kind=op_kind,
            asset=asset,
            amount_native=0.0,  # dummy for journal params — real amount is MAX
            call=call,
        )

    async def withdraw(self, asset: str, amount_native: float) -> OpResult:
        """Withdraw an EXACT amount. Prefer `withdraw_all` to close a round trip.

        Asking for the exact amount that was supplied reverts intermittently:
        Aave stores deposits as `scaledBalance = amount / liquidityIndex` and
        rounds DOWN on both that division and the multiplication back, so an
        aToken balance can settle one unit below the deposit (5.000000 USDC
        supplied reads back as 4.999999). Whether it does depends on the index
        at deposit time — which is why the same sequence passes one day and
        reverts the next. See memory/aave_findings.md.
        """
        self._guard.check("aave_withdraw", self._estimate_notional(asset, amount_native))
        return await self._pool_write(
            op_kind="aave_withdraw",
            asset=asset,
            amount_native=amount_native,
            build_call=lambda raw_amount: self._pool.functions.withdraw(
                AsyncWeb3.to_checksum_address(asset),
                raw_amount,
                self._master,
            ),
        )

    async def withdraw_all(self, asset: str, notional_hint: float) -> OpResult:
        """Withdraw the FULL aToken balance via the MAX_UINT256 sentinel.

        The exact-amount `withdraw` cannot close a round trip reliably (see its
        docstring): it reverts whenever Aave's rounding leaves the balance one
        unit short. MAX_UINT256 tells Aave "everything I hold", which is both
        rounding-proof and the only amount that leaves no dust collateral.

        Mirror of `repay_all`, with the same caveat: this empties the entire
        USDC collateral, not just this cycle's deposit. That is correct for the
        M1 tracer, which is the only supplier during the marche a blanc. Once
        the real USDC cushion exists (M2), a cycle must withdraw its own
        deposit only — read the aToken balance and pass it to `withdraw`, which
        is safe in that direction because the balance only grows with interest.

        `notional_hint` is what the guard sees: MAX_UINT256 as a notional would
        blow the `max_op_usd` cap on every call, so callers pass the amount
        they supplied.
        """
        max_uint = 2**256 - 1
        op_kind: AaveOpKind = "aave_withdraw"
        self._guard.check(op_kind, notional_usd=self._estimate_notional(asset, notional_hint))

        call = self._pool.functions.withdraw(
            AsyncWeb3.to_checksum_address(asset),
            max_uint,
            self._master,
        )
        return await self._journal_and_send(
            op_kind=op_kind,
            asset=asset,
            amount_native=0.0,  # dummy for journal params — real amount is MAX
            call=call,
        )

    # --- Internal plumbing ----------------------------------------------------

    async def _erc20_write(
        self,
        *,
        op_kind: AaveOpKind,
        asset: str,
        amount_native: float,
        build_call: Any,
    ) -> OpResult:
        contract = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(asset),
            abi=_ERC20_MUT_ABI,
        )
        decimals: int = await contract.functions.decimals().call()
        raw_amount = to_raw(amount_native, decimals, _ROUNDING[op_kind])
        call = build_call(contract, raw_amount)
        return await self._journal_and_send(
            op_kind=op_kind,
            asset=asset,
            amount_native=amount_native,
            call=call,
        )

    async def _pool_write(
        self,
        *,
        op_kind: AaveOpKind,
        asset: str,
        amount_native: float,
        build_call: Any,
    ) -> OpResult:
        # Reuse the asset ERC20 to read decimals.
        token = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(asset),
            abi=_ERC20_MUT_ABI,
        )
        decimals: int = await token.functions.decimals().call()
        raw_amount = to_raw(amount_native, decimals, _ROUNDING[op_kind])
        call = build_call(raw_amount)
        return await self._journal_and_send(
            op_kind=op_kind,
            asset=asset,
            amount_native=amount_native,
            call=call,
        )

    async def _refuse_if_underfunded(
        self,
        op_kind: AaveOpKind,
        asset: str,
        *,
        needed: float | None,
    ) -> None:
        """Refuse an operation the wallet demonstrably cannot fund.

        `needed=None` means "the full outstanding debt", which is what
        `repay_all` spends: MAX_UINT256 does not mean "whatever I have", Aave
        pulls the whole debt. Resolving it from the same read keeps the guard
        to one round trip.

        The guard refuses only on a balance it actually read. A failed read
        must NOT block the operation: on an emergency path a repay that might
        have worked is worth more than a refusal based on ignorance, and the
        transaction still carries its own revert as a second line of defence.
        We block what we know to be impossible, never what we merely failed to
        check.
        """
        if self._balances is None:
            log.warning(
                "balance_check_absent",
                message=f"{op_kind}: aucun lecteur de soldes câblé — opération non vérifiée",
                op_kind=op_kind,
            )
            return
        try:
            balances = await self._balances.read_token_balances(asset)
        except failure.OPERATIONAL_ERRORS:
            log.warning(
                "balance_check_unavailable",
                message=f"{op_kind}: solde libre illisible — opération tentée quand même",
                op_kind=op_kind,
            )
            return
        required = balances.variable_debt_balance if needed is None else needed
        if required <= 0.0:
            return
        if balances.wallet_balance < required:
            raise InsufficientBalance(
                f"{op_kind}: le portefeuille porte {balances.wallet_balance:.6f} "
                f"et l'opération en demande {required:.6f}",
            )

    async def _journal_and_send(
        self,
        *,
        op_kind: AaveOpKind,
        asset: str,
        amount_native: float,
        call: Any,
    ) -> OpResult:
        # No guard check here: every public method passes the guard once, first,
        # before any network read (its contract). Checking again here counted
        # each MAX_UINT256 operation twice against the hourly limit — the second
        # time with a notional of 0, which no cap could ever refuse.
        intent_id = deterministic_id(
            op_kind,
            asset,
            f"{amount_native:.9f}",
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        params = {"asset": asset, "amount_native": amount_native, "op_kind": op_kind}
        await self._insert_pending_intent(intent_id, op_kind, params)

        start = now_perf()

        if self._config.tracer.dry_run:
            log.info(
                "op_dry_run",
                message=f"{op_kind}: dry-run, aucune transaction envoyée",
                op_kind=op_kind,
                asset=asset,
                amount=amount_native,
            )
            duration_ms = elapsed_ms(start)
            await self._store.record_latency(
                measurement_path(f"path.{op_kind}", dry_run=True),
                duration_ms,
            )
            await self._mark_intent_status(intent_id, "confirmed", None)
            return OpResult(
                intent_id=intent_id,
                tx_hash=None,
                status="dry_run",
                duration_ms=duration_ms,
                gas_used=None,
            )

        try:
            tx = await call.build_transaction(
                {
                    "from": self._master,
                    "nonce": await self._w3.eth.get_transaction_count(self._master),
                    "chainId": self._chain_id,
                },
            )
            tx["gas"] = with_gas_margin(int(tx["gas"]))
            signed = self._w3.eth.account.sign_transaction(tx, private_key=self._pkey())
            tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash)
        except Exception as e:
            cause = failure.from_exception(e)
            await self._mark_intent_status(intent_id, "failed", None)
            await self._store.record_intent_failure(intent_id, cause.journal_entry())
            log.exception(
                "op_send_failed",
                message=f"{op_kind}: envoi ou attente de reçu en échec",
                intent_id=intent_id,
                failure=cause.journal_entry(),
            )
            raise

        duration_ms = elapsed_ms(start)
        await self._store.record_latency(f"path.{op_kind}", duration_ms)
        gas_used = int(receipt.get("gasUsed", 0))
        status = int(receipt.get("status", 0))
        if status != 1:
            cause = await failure.diagnose_revert(
                call=call,
                receipt=receipt,
                tx_hash=tx_hash.hex(),
                sender=self._master,
                gas_used=gas_used,
                gas_limit=int(tx["gas"]),
            )
            await self._mark_intent_status(intent_id, "failed", [tx_hash.hex()])
            await self._store.record_intent_failure(intent_id, cause.journal_entry())
            log.error(
                "op_reverted",
                message=f"{op_kind}: transaction reverted — {cause.journal_entry()}",
                intent_id=intent_id,
                tx_hash=tx_hash.hex(),
                failure=cause.journal_entry(),
            )
            return OpResult(
                intent_id=intent_id,
                tx_hash=tx_hash.hex(),
                status="failed",
                duration_ms=duration_ms,
                gas_used=gas_used,
            )

        await self._mark_intent_status(intent_id, "confirmed", [tx_hash.hex()])
        log.info(
            "op_confirmed",
            message=f"{op_kind} confirmée en {duration_ms:.1f} ms",
            op_kind=op_kind,
            duration_ms=duration_ms,
            gas_used=gas_used,
            tx_hash=tx_hash.hex(),
        )
        return OpResult(
            intent_id=intent_id,
            tx_hash=tx_hash.hex(),
            status="confirmed",
            duration_ms=duration_ms,
            gas_used=gas_used,
        )

    def _pkey(self) -> str:
        # The private key is opt-in — a wiring error must fail LOUDLY rather
        # than silently attempt to sign with an empty string.
        if not self._private_key:
            raise NotImplementedError(
                "private key not provided — pass private_key= to constructor "
                "(the CLI wires it from .env via --live-micro-ops)",
            )
        return self._private_key

    def _estimate_notional(self, asset: str, amount_native: float) -> float:
        # Stables assumed at 1 $. Volatile tokens (wstETH) at a conservative 3 000 $.
        # This is ONLY used for the safety cap; a rough over-estimate is fine.
        if asset.lower() == self._config.venues.wsteth_address.lower():
            return amount_native * 3_000.0
        return amount_native

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
            VALUES (?, ?, ?, 0, ?, 'micro-op M1-B2', 'pending', ?)
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
