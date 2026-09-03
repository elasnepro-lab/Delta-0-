"""CLI entry point for the bot.

Commands:
    delta0 config-check     validate YAML, no network calls              (M0)
    delta0 status           snapshot both venues, read-only               (M0)
    delta0 tracer           M1 marche à blanc — journal des tirs à blanc  (M1)
    delta0 report           tirs à blanc + p95 des 5 chemins vs budget    (M1)

Later milestones will add: run, deflate, unwind.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from web3 import AsyncHTTPProvider, AsyncWeb3

from delta0 import __version__
from delta0.config import Config, load_config
from delta0.decision import target_state
from delta0.executor import AaveTraceExecutor
from delta0.hl_executor import HLTraceExecutor
from delta0.latency import (
    PathVerdict,
    evaluate_all,
    m1_acceptance_met,
    needs_prudent_mode,
    path_meets_m1,
)
from delta0.logging import configure_logging, get_logger, new_run_id
from delta0.reconcile import ReconcileReport, reconcile_at_boot
from delta0.safety import ALLOWED_OP_KINDS, MicroOpsGuard
from delta0.settings import Settings, load_settings
from delta0.state import StateStore
from delta0.tracer import TracerLoop
from delta0.venues.aave import AaveReader
from delta0.venues.bridge import BridgeExecutor
from delta0.venues.hl_stream import HyperliquidStream
from delta0.venues.hyperliquid import HyperliquidReader
from delta0.watchdog import Watchdog
from delta0.watcher import LiveWatcher

ARBITRUM_CHAIN_ID = 42161

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Delta-0- — bot Montage C (delta-neutre wstETH / short ETH).",
)
console = Console()

_DEFAULT_CONFIG = Path("config.yaml")
_DEFAULT_DB = Path("data/delta0.db")


def _parse_duration(spec: str) -> float:
    """Parse durations like '30s', '10m', '2h', '7d' into seconds."""
    spec = spec.strip().lower()
    if not spec:
        raise typer.BadParameter("empty duration")
    unit = spec[-1]
    try:
        value = float(spec[:-1]) if unit in "smhd" else float(spec)
    except ValueError as e:
        raise typer.BadParameter(f"invalid duration {spec!r}") from e
    match unit:
        case "s":
            return value
        case "m":
            return value * 60
        case "h":
            return value * 3600
        case "d":
            return value * 86400
        case _:
            return value


@app.command()
def version() -> None:
    """Print the bot version."""
    console.print(f"delta0 {__version__}")


@app.command("config-check")
def config_check(
    config: Annotated[Path, typer.Option("--config", "-c", exists=False)] = _DEFAULT_CONFIG,
) -> None:
    """Validate the YAML config against the schema — no network calls."""
    cfg = load_config(config)
    console.print(
        Panel.fit(
            f"[bold green]Configuration valide[/bold green]\n"
            f"capital = ${cfg.capital_usd:,.0f}   "
            f"levier = {cfg.short_leverage}x   "
            f"LTV cible = {cfg.target_ltv:.0%}   "
            f"exposition = {cfg.exposure_mult:.2f}x   "
            f"mode = {cfg.mode.value}",
            title="config.yaml",
        ),
    )


@app.command()
def status(
    config: Annotated[Path, typer.Option("--config", "-c", exists=False)] = _DEFAULT_CONFIG,
    json_out: Annotated[bool, typer.Option("--json", help="Emit one JSON line to stdout.")] = False,
) -> None:
    """Read-only snapshot of both venues + target-state comparison."""
    cfg = load_config(config)
    settings = load_settings()

    configure_logging(cfg.mode)
    log = get_logger("status")
    run_id = new_run_id()
    log.info("status_start", message="démarrage lecture read-only", run_id=run_id)

    result = asyncio.run(_gather_status(cfg, settings))

    if json_out:
        typer.echo(json.dumps(result, default=_json_default))
        return

    _render_status(cfg, result)


async def _gather_status(cfg: Config, settings: Settings) -> dict[str, object]:
    log = get_logger("status.gather")

    # Aave leg.
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.arbitrum_rpc_primary))
    aave = AaveReader(
        web3=w3,
        pool_address=cfg.venues.aave_pool,
        user_address=settings.bot_master_address,
    )
    account = await aave.read_account_data()
    wsteth_bal = await aave.read_token_balances(cfg.venues.wsteth_address)
    usdc_bal = await aave.read_token_balances(cfg.venues.usdc_address)
    gas_eth = await aave.read_gas_balance_eth()

    if account.emode != 0:
        log.warning(
            "aave_emode_nonzero",
            message=f"e-mode Aave = {account.emode}, attendu 0",
            emode=account.emode,
        )

    # Hyperliquid leg.
    hl = HyperliquidReader(cfg.venues.hl_api, user_address=settings.bot_master_address)
    meta = await hl.read_market_meta("ETH")
    position = await hl.read_position("ETH")
    funding_30d = await hl.read_funding_avg_30d("ETH")

    equity = (
        account.total_collateral_usd
        + (position.isolated_margin_usd if position else 0.0)
        - account.total_debt_usd
    )
    targets = target_state(equity=max(equity, 1.0), config=cfg) if equity > 0 else None

    return {
        "ts": datetime.now(UTC).isoformat(),
        "aave": {
            "collateral_usd": account.total_collateral_usd,
            "debt_usd": account.total_debt_usd,
            "hf": account.health_factor,
            "ltv": (
                account.total_debt_usd / account.total_collateral_usd
                if account.total_collateral_usd > 0
                else 0.0
            ),
            "lt": account.liquidation_threshold,
            "emode": account.emode,
            "wsteth_balance": wsteth_bal.atoken_balance,
            "usdc_supply_balance": usdc_bal.atoken_balance,
            "usdc_debt_balance": usdc_bal.variable_debt_balance,
            "gas_eth": gas_eth,
        },
        "hyperliquid": {
            "coin": "ETH",
            "mark_price": meta.mark_price,
            "maintenance_margin_ratio_estimated": meta.maintenance_margin_ratio,
            "position_size": position.size_signed if position else 0.0,
            "isolated_margin_usd": position.isolated_margin_usd if position else 0.0,
            "leverage": position.leverage if position else 0,
            "funding_30d_annualized": funding_30d,
        },
        "equity_usd": equity,
        "targets": (
            {
                "spot_target_usd": targets.spot_target_usd,
                "notional_target_usd": targets.notional_target_usd,
                "margin_target_usd": targets.margin_target_usd,
                "debt_target_usd": targets.debt_target_usd,
            }
            if targets
            else None
        ),
    }


def _render_status(cfg: Config, data: dict[str, object]) -> None:
    aave = data["aave"]
    hl = data["hyperliquid"]
    targets = data["targets"]
    assert isinstance(aave, dict)
    assert isinstance(hl, dict)

    aave_table = Table(title="Aave v3 Arbitrum (lecture seule)", show_header=True)
    aave_table.add_column("Poste")
    aave_table.add_column("Valeur", justify="right")
    aave_table.add_row("Collatéral", f"${aave['collateral_usd']:,.2f}")
    aave_table.add_row("Dette", f"${aave['debt_usd']:,.2f}")
    aave_table.add_row("LTV", f"{aave['ltv']:.4f}")
    aave_table.add_row("LT (on-chain)", f"{aave['lt']:.4f}")
    aave_table.add_row(
        "HF",
        "inf" if aave["hf"] == float("inf") else f"{aave['hf']:.4f}",
    )
    aave_table.add_row("e-mode", str(aave["emode"]))
    aave_table.add_row("wstETH aToken", f"{aave['wsteth_balance']:.6f}")
    aave_table.add_row("USDC coussin", f"{aave['usdc_supply_balance']:,.2f}")
    aave_table.add_row("USDC dette", f"{aave['usdc_debt_balance']:,.2f}")
    aave_table.add_row("Gaz ETH", f"{aave['gas_eth']:.6f}")

    hl_table = Table(title="Hyperliquid (lecture seule)", show_header=True)
    hl_table.add_column("Poste")
    hl_table.add_column("Valeur", justify="right")
    hl_table.add_row("Coin", str(hl["coin"]))
    hl_table.add_row("Prix mark", f"${hl['mark_price']:,.2f}")
    hl_table.add_row("Taille position", f"{hl['position_size']:.6f}")
    hl_table.add_row("Marge isolée", f"${hl['isolated_margin_usd']:,.2f}")
    hl_table.add_row("Levier", str(hl["leverage"]))
    hl_table.add_row("Funding 30j annualisé", f"{hl['funding_30d_annualized']:.4%}")

    console.print(aave_table)
    console.print(hl_table)

    if targets is not None:
        assert isinstance(targets, dict)
        t_table = Table(title="État cible (solveur)", show_header=True)
        t_table.add_column("Cible")
        t_table.add_column("Valeur", justify="right")
        t_table.add_row("Spot", f"${targets['spot_target_usd']:,.2f}")
        t_table.add_row("Notionnel", f"${targets['notional_target_usd']:,.2f}")
        t_table.add_row("Marge", f"${targets['margin_target_usd']:,.2f}")
        t_table.add_row("Dette", f"${targets['debt_target_usd']:,.2f}")
        console.print(t_table)
    else:
        console.print("[yellow]Équité nulle ou négative — pas de cible à calculer.[/yellow]")

    console.print(
        Panel.fit(
            f"Mode: [bold]{cfg.mode.value}[/bold] | "
            f"Équité observée: [bold]${data['equity_usd']:,.2f}[/bold]",
            title="Récapitulatif",
        ),
    )


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if obj == float("inf"):
        return "inf"
    raise TypeError(f"unserializable: {type(obj).__name__}")


# --- M1 commands --------------------------------------------------------------


@app.command()
def tracer(
    config: Annotated[Path, typer.Option("--config", "-c")] = _DEFAULT_CONFIG,
    db: Annotated[Path, typer.Option("--db")] = _DEFAULT_DB,
    duration: Annotated[
        str,
        typer.Option("--duration", "-d", help="ex: 30s, 10m, 2h, 7d — vide = infini"),
    ] = "",
    cadence: Annotated[float, typer.Option("--cadence", help="secondes entre snapshots")] = 5.0,
    live_micro_ops: Annotated[
        bool,
        typer.Option(
            "--live-micro-ops",
            help=(
                "Active les micro-ops réelles (Aave / HL / bridge). "
                "Requiert config.tracer.dry_run=false ET .env avec la clé privée."
            ),
        ),
    ] = False,
    confirm: Annotated[
        list[str] | None,
        typer.Option(
            "--confirm",
            help=(
                "Autorise la première exécution d'un op_kind (ex: aave_supply). "
                f"Valeurs valides: {', '.join(sorted(ALLOWED_OP_KINDS))}"
            ),
        ),
    ] = None,
    rehearse: Annotated[
        bool,
        typer.Option(
            "--rehearse",
            help=(
                "Répétition à blanc : câble les 3 executors en gardant "
                "config.tracer.dry_run=true. Aucune transaction, aucune clé "
                "privée. Exerce guard, journal d'intentions et mesure de latence."
            ),
        ),
    ] = False,
    no_ws: Annotated[
        bool,
        typer.Option(
            "--no-ws",
            help=(
                "Désactive le flux WS Hyperliquid (mark price REST seulement, "
                "aucune détection de liquidation P1). Dépannage uniquement."
            ),
        ),
    ] = False,
) -> None:
    """M1 marche à blanc — observe, décide, journalise (aucune exécution par défaut)."""
    cfg = load_config(config)

    # Coherence of the execution flags against the config is checked FIRST,
    # before the environment is read. A contradictory combination is wrong
    # whatever `.env` contains, and a refusal that needs a populated `.env`
    # to be reached is a refusal that cannot be tested.
    _check_execution_flags(cfg, live_micro_ops=live_micro_ops, rehearse=rehearse)

    settings = load_settings()
    configure_logging(cfg.mode)
    log = get_logger("tracer")
    run_id = new_run_id()
    duration_s = _parse_duration(duration) if duration else None
    confirmed_kinds = list(confirm or [])
    log.info(
        "tracer_boot",
        message=(
            f"démarrage TRACER (cadence {cadence}s, durée {duration or 'infinie'}, "
            f"live_micro_ops={live_micro_ops}, rehearse={rehearse}, ws={not no_ws})"
        ),
        run_id=run_id,
        live_micro_ops=live_micro_ops,
        rehearse=rehearse,
        confirmed_kinds=confirmed_kinds,
        ws_enabled=not no_ws,
    )
    if rehearse:
        console.print(
            "[bold yellow]RÉPÉTITION[/bold yellow] : executors câblés, "
            "dry_run actif, aucune clé privée chargée. Les latences sont "
            "enregistrées sous [bold]dry.path.*[/bold] et n'entrent pas "
            "dans le rapport des chemins critiques.",
        )
    asyncio.run(
        _run_tracer(
            cfg,
            settings,
            db,
            duration_s,
            cadence,
            live_micro_ops,
            confirmed_kinds,
            use_ws=not no_ws,
            rehearse=rehearse,
        ),
    )


def _check_execution_flags(cfg: Config, *, live_micro_ops: bool, rehearse: bool) -> None:
    """Refuse contradictory execution flags before anything else happens.

    Pure: config + flags in, `typer.Exit` or nothing out. No env, no network,
    no filesystem — so every refusal is reachable in a test.
    """
    if live_micro_ops and rehearse:
        console.print(
            "[bold red]REFUS[/bold red]: --live-micro-ops et --rehearse "
            "s'excluent. La répétition ne doit jamais pouvoir devenir un tir réel.",
        )
        raise typer.Exit(code=6)
    if live_micro_ops and cfg.tracer.dry_run:
        console.print(
            "[bold red]REFUS[/bold red]: --live-micro-ops passé mais "
            "config.tracer.dry_run=true. Bascule dry_run à false d'abord.",
        )
        raise typer.Exit(code=2)
    if rehearse and not cfg.tracer.dry_run:
        console.print(
            "[bold red]REFUS[/bold red]: --rehearse passé mais "
            "config.tracer.dry_run=false. Une répétition qui envoie des "
            "transactions n'est pas une répétition.",
        )
        raise typer.Exit(code=6)


async def _run_tracer(
    cfg: Config,
    settings: Settings,
    db_path: Path,
    duration_s: float | None,
    cadence: float,
    live_micro_ops: bool,
    confirmed_kinds: list[str],
    use_ws: bool,
    rehearse: bool = False,
) -> None:
    store = StateStore(db_path)
    await store.open()

    w3 = AsyncWeb3(AsyncHTTPProvider(settings.arbitrum_rpc_primary))
    aave = AaveReader(
        web3=w3,
        pool_address=cfg.venues.aave_pool,
        user_address=settings.bot_master_address,
    )
    hl = HyperliquidReader(cfg.venues.hl_api, user_address=settings.bot_master_address)
    watchdog = Watchdog(config=cfg.watchdog, project_root=Path.cwd())

    # WS feed: fresher mark price than REST polling, and the only source of
    # HL liquidation events (P1). A WS that refuses to start degrades the
    # tracer to REST-only rather than aborting the run — the watchdog will
    # see the staleness and say so.
    stream = _start_hl_stream(cfg, settings) if use_ws else None
    watcher = LiveWatcher(config=cfg, aave=aave, hl=hl, watchdog=watchdog, stream=stream)

    try:
        # README §13: reconcile the journal against on-chain reality BEFORE
        # any new action. In dry-run a failure is only a warning; with live
        # micro-ops armed, refusing to start is the safe call.
        ok = await _reconcile_boot(store, watcher, strict=live_micro_ops)
        if not ok:
            raise typer.Exit(code=5)

        aave_exec = None
        hl_exec = None
        bridge_exec = None
        if live_micro_ops or rehearse:
            aave_exec, hl_exec, bridge_exec = _wire_micro_op_executors(
                cfg=cfg,
                settings=settings,
                store=store,
                w3=w3,
                confirmed_kinds=confirmed_kinds,
                rehearse=rehearse,
            )

        loop = TracerLoop(
            watcher=watcher,
            watchdog=watchdog,
            store=store,
            config=cfg,
            cadence_s=cadence,
            stream=stream,
            aave_executor=aave_exec,
            hl_executor=hl_exec,
            bridge_executor=bridge_exec,
        )
        n = await loop.run(duration_s=duration_s)
    finally:
        if stream is not None:
            stream.stop()
        await store.close()
    console.print(f"[bold green]TRACER terminé[/bold green] — {n} tirs à blanc journalisés.")
    console.print("Rapport : [bold]delta0 report[/bold]")


def _start_hl_stream(cfg: Config, settings: Settings) -> HyperliquidStream | None:
    """Start the HL WebSocket, or return None if it cannot be started."""
    log = get_logger("tracer")
    stream = HyperliquidStream(
        api_url=cfg.venues.hl_api,
        user_address=settings.bot_master_address,
    )
    try:
        stream.start()
    except Exception:
        log.exception(
            "hl_stream_start_failed",
            message="WS Hyperliquid indisponible — repli sur les lectures REST seules",
        )
        return None
    return stream


async def _reconcile_boot(store: StateStore, watcher: LiveWatcher, *, strict: bool) -> bool:
    """Run the boot reconciliation. Returns False when the run must not start.

    `strict` (live micro-ops armed) turns a failed snapshot or any warning
    into a refusal: we never fire a real transaction against a state we could
    not verify.
    """
    log = get_logger("tracer")
    try:
        snap = await watcher.snapshot()
    except Exception:
        log.exception(
            "reconcile_snapshot_failed",
            message="snapshot de réconciliation impossible au démarrage",
        )
        if strict:
            console.print(
                "[bold red]REFUS[/bold red]: réconciliation impossible "
                "(snapshot en échec) alors que --live-micro-ops est armé.",
            )
            return False
        console.print(
            "[yellow]Réconciliation ignorée[/yellow] : snapshot indisponible. "
            "La boucle démarre quand même (mode observation).",
        )
        return True

    report: ReconcileReport = await reconcile_at_boot(store, snap)
    _render_reconcile(report)
    if report.warnings and strict:
        console.print(
            "[bold red]REFUS[/bold red]: la réconciliation a levé "
            f"{len(report.warnings)} avertissement(s) et --live-micro-ops est armé. "
            "Traite-les avant de rejouer.",
        )
        return False
    return True


def _render_reconcile(report: ReconcileReport) -> None:
    drift = f"{report.anchor_drift_pct:+.2%}" if report.anchor_drift_pct is not None else "n/a"
    anchor = f"{report.anchor_journal:.2f}" if report.anchor_journal is not None else "aucune"
    lines = [
        f"Ancre journalisée : {anchor}   mark : {report.anchor_current_mark:.2f}   "
        f"dérive : {drift}",
        f"Dette on-chain : {report.debt_on_chain:.2f} $",
    ]
    if report.warnings:
        lines.extend(f"[yellow]![/yellow] {w}" for w in report.warnings)
    console.print(
        Panel(
            "\n".join(lines),
            title="Réconciliation au démarrage (README §13)",
            style="yellow" if report.warnings else "green",
        ),
    )


def _wire_micro_op_executors(
    *,
    cfg: Config,
    settings: Settings,
    store: StateStore,
    w3: AsyncWeb3,  # type: ignore[type-arg]
    confirmed_kinds: list[str],
    rehearse: bool,
) -> tuple[AaveTraceExecutor, HLTraceExecutor, BridgeExecutor]:
    """Instantiate the three micro-op executors.

    Live (`--live-micro-ops`, dry_run=false): loads the .env private key. It
    stays in memory as a plain string only inside the executors; it is never
    logged and never journaled.

    Rehearsal (`--rehearse`, dry_run=true): wires the SAME objects with no
    key at all, and a signing path that raises. Every read runs for real
    (mark price, balances); every write short-circuits in the executor's
    dry-run branch. If that short-circuit ever failed, the rehearsal would
    crash rather than sign — which is the whole point of handing it nothing
    to sign with.
    """
    # Lazy on purpose: a DRY_RUN run must never import the signing libraries.
    from eth_account import Account  # noqa: PLC0415
    from hyperliquid.exchange import Exchange  # noqa: PLC0415
    from hyperliquid.info import Info as HLInfo  # noqa: PLC0415

    pkey: str | None
    if rehearse:
        pkey = None
    else:
        pkey = settings.bot_master_private_key.get_secret_value()
        if not pkey or pkey.startswith("REPLACE"):
            console.print(
                "[bold red]REFUS[/bold red]: BOT_MASTER_PRIVATE_KEY manquant "
                "ou placeholder dans .env.",
            )
            raise typer.Exit(code=3)

    guard = MicroOpsGuard(config=cfg.tracer, project_root=Path.cwd())
    for kind in confirmed_kinds:
        if kind not in ALLOWED_OP_KINDS:
            console.print(f"[bold red]REFUS[/bold red]: op_kind inconnu: {kind!r}")
            raise typer.Exit(code=4)
        guard.confirm_kind(kind)

    aave = AaveTraceExecutor(
        web3=w3,
        config=cfg,
        store=store,
        guard=guard,
        master_address=settings.bot_master_address,
        chain_id=ARBITRUM_CHAIN_ID,
        private_key=pkey,
    )

    hl_info = HLInfo(cfg.venues.hl_api, skip_ws=True)

    async def _mark_price(coin: str) -> float:
        mids = hl_info.all_mids()
        raw = mids.get(coin)
        return float(raw) if raw is not None else 0.0

    def _make_exchange() -> object:
        if pkey is None:
            raise NotImplementedError(
                "répétition (--rehearse) : aucun ordre Hyperliquid ne doit être construit. "
                "Le court-circuit dry-run de l'executor a été franchi — c'est un bug.",
            )
        return Exchange(Account.from_key(pkey), cfg.venues.hl_api)

    # Size/price grids come from the exchange meta, not a constant: HL rejects
    # an order whose size carries more decimals than the asset allows, and the
    # SDK raises locally so the order never leaves the process. Cached — the
    # universe changes far more slowly than the tracer loops.
    _sz_decimals_cache: dict[str, int] = {}

    async def _size_decimals(coin: str) -> int:
        if coin not in _sz_decimals_cache:
            meta = await asyncio.to_thread(hl_info.meta)
            for asset in meta.get("universe", []):
                name = asset.get("name")
                if name is not None:
                    _sz_decimals_cache[name] = int(asset.get("szDecimals", 4))
        return _sz_decimals_cache.get(coin, 4)

    hl_exec = HLTraceExecutor(
        config=cfg,
        store=store,
        guard=guard,
        exchange_factory=_make_exchange,
        get_mark_price=_mark_price,
        get_size_decimals=_size_decimals,
    )

    bridge = BridgeExecutor(
        web3=w3,
        config=cfg,
        store=store,
        guard=guard,
        master_address=settings.bot_master_address,
        chain_id=ARBITRUM_CHAIN_ID,
        hl_exchange_factory=_make_exchange,
        hl_info=hl_info,
        private_key=pkey,
    )

    return aave, hl_exec, bridge


@app.command()
def report(
    db: Annotated[Path, typer.Option("--db")] = _DEFAULT_DB,
    config: Annotated[Path, typer.Option("--config", "-c")] = _DEFAULT_CONFIG,
) -> None:
    """Rapport TRACER : tirs à blanc + p50/p95 des 5 chemins critiques vs budget."""
    asyncio.run(_run_report(db, config))


async def _run_report(db_path: Path, config_path: Path) -> None:
    cfg = load_config(config_path)
    store = StateStore(db_path)
    await store.open()
    try:
        total = await store.count_shadow_intents()
        by_prio = await store.shadow_intents_by_priority()
        stats_by_path = await store.latency_stats_all()
    finally:
        await store.close()

    factor = cfg.watchdog.latency_budget_factor
    verdicts = evaluate_all(stats_by_path, budget_factor=factor)

    _render_shadow_intents(total, by_prio)
    _render_critical_paths(verdicts, factor)
    _render_raw_latencies(stats_by_path)
    _render_m1_verdict(verdicts, factor)


_MS_PER_S = 1_000.0
_MS_PER_MIN = 60_000.0
# Below this, show two decimals: the decision loop is sub-millisecond.
_MS_SUBMILLI = 10.0


def _fmt_ms(ms: float) -> str:
    """Render a duration at the scale a human reads it at (ms / s / min)."""
    if ms < _MS_SUBMILLI:
        # Rounding the decision loop to "0 ms" would hide the one number that
        # proves decision.py does no I/O.
        return f"{ms:.2f} ms"
    if ms < _MS_PER_S:
        return f"{ms:.0f} ms"
    if ms < _MS_PER_MIN:
        return f"{ms / _MS_PER_S:.1f} s"
    minutes, seconds = divmod(ms / _MS_PER_S, 60)
    return f"{int(minutes)} min {seconds:02.0f} s"


_VERDICT_STYLE: dict[str, str] = {
    "OK": "bold green",
    "DEPASSE": "bold yellow",
    "PRUDENT": "bold red",
    "INCOMPLET": "yellow",
    "AUCUN": "dim",
}


def _render_shadow_intents(total: int, by_prio: dict[int, int]) -> None:
    table = Table(title=f"Tirs à blanc par priorité (total: {total})")
    table.add_column("Priorité")
    table.add_column("Compte", justify="right")
    for prio_val in sorted(by_prio):
        table.add_row(f"P{prio_val}", str(by_prio[prio_val]))
    if not by_prio:
        table.add_row("—", "0")
    console.print(table)


def _render_critical_paths(verdicts: list[PathVerdict], factor: float) -> None:
    """The M1 deliverable: p95 of each critical path against its budget."""
    table = Table(title=f"Chemins critiques (README §7) — p95 vs budget (facteur {factor})")
    table.add_column("P")
    table.add_column("Chemin")
    table.add_column("Venue")
    table.add_column("n", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("Budget", justify="right")
    table.add_column("p95/bud", justify="right")
    table.add_column("Verdict")

    for v in verdicts:
        style = _VERDICT_STYLE[v.verdict]
        measured = v.verdict != "AUCUN"
        table.add_row(
            v.path.key,
            v.path.label,
            v.path.venue,
            str(v.samples),
            _fmt_ms(v.p50_ms) if measured else "—",
            _fmt_ms(v.p95_ms) if measured else "—",
            _fmt_ms(v.path.budget_ms),
            f"{v.budget_ratio:.2f}x" if measured else "—",
            f"[{style}]{v.verdict}[/{style}]",
        )
    console.print(table)

    # Explain every non-OK row rather than leaving the operator to guess.
    for v in verdicts:
        if v.missing:
            console.print(
                f"  [dim]{v.path.key}: aucune mesure pour "
                f"{', '.join(m.removeprefix('path.') for m in v.missing)}[/dim]",
            )
        if v.path.unmeasured:
            console.print(
                f"  [dim]{v.path.key}: jambe non mesurable en M1 — "
                f"{', '.join(v.path.unmeasured)} (venues/swap.py est un stub)[/dim]",
            )


def _render_raw_latencies(stats_by_path: dict[str, dict[str, float]]) -> None:
    """Every raw measurement, including the non-critical snapshot/decision loops."""
    table = Table(title="Latences brutes par micro-op")
    table.add_column("Mesure")
    table.add_column("Compte", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("max", justify="right")
    for name in sorted(stats_by_path):
        stats = stats_by_path[name]
        table.add_row(
            name.removeprefix("path."),
            f"{int(stats['count'])}",
            _fmt_ms(stats["p50"]),
            _fmt_ms(stats["p95"]),
            _fmt_ms(stats["max"]),
        )
    if not stats_by_path:
        table.add_row("—", "0", "—", "—", "—")
    console.print(table)


def _render_m1_verdict(verdicts: list[PathVerdict], factor: float) -> None:
    if m1_acceptance_met(verdicts):
        # Name the exempted legs in the pass message. A green panel that hides
        # what it excused is worse than no panel: the operator signing off on
        # M1 has to see exactly what was NOT measured.
        excused = [
            f"{v.path.key} ({', '.join(v.path.unmeasured)})" for v in verdicts if v.verdict != "OK"
        ]
        note = (
            f"\nJambes non mesurables en M1, exemptées : {'; '.join(excused)}." if excused else ""
        )
        console.print(
            Panel(
                "Les 5 chemins critiques tiennent leur budget (p95 <= budget) "
                f"sur tout ce que M1 peut mesurer. Critère de vitesse M1 satisfait.{note}",
                title="Vitesse (README §12)",
                style="bold green",
            ),
        )
    else:
        blockers = [f"{v.path.key}={v.verdict}" for v in verdicts if not path_meets_m1(v)]
        console.print(
            Panel(
                "Critère de vitesse M1 NON satisfait — "
                f"{', '.join(blockers)}.\n"
                "Un chemin AUCUN/INCOMPLET signifie qu'il manque des micro-ops, "
                "pas que le chemin est lent.",
                title="Vitesse (README §12)",
                style="bold yellow",
            ),
        )
    if needs_prudent_mode(verdicts):
        slow = [v.path.key for v in verdicts if v.verdict == "PRUDENT"]
        console.print(
            Panel(
                f"p95 > budget x {factor} sur {', '.join(slow)} — README §11 impose le "
                "mode prudent : re-centrage anticipé à +3 % / -4,5 %.",
                title="Mode prudent",
                style="bold red",
            ),
        )


if __name__ == "__main__":
    app()
