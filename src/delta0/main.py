"""CLI entry point for the bot.

Commands (M0):
    delta0 config-check [--config PATH]     validate YAML, no network calls
    delta0 status       [--config PATH]     connect read-only to Aave + HL, print snapshot

Later milestones will add: run, deflate, unwind, status --json.
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
from delta0.logging import configure_logging, get_logger, new_run_id
from delta0.settings import Settings, load_settings
from delta0.venues.aave import AaveReader
from delta0.venues.hyperliquid import HyperliquidReader

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Delta-0- — bot Montage C (delta-neutre wstETH / short ETH).",
)
console = Console()

_DEFAULT_CONFIG = Path("config.yaml")


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
        "∞" if aave["hf"] == float("inf") else f"{aave['hf']:.4f}",
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


if __name__ == "__main__":
    app()
