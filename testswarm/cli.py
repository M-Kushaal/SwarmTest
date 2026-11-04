"""Command line.

    python -m testswarm list                         show agents in personas.yaml
    python -m testswarm check                        validate config.yaml + personas.yaml
    python -m testswarm run --agent retired-teacher  run one agent
    python -m testswarm run -a shopper -a retired-teacher   run several in parallel
    python -m testswarm run --all                    run every agent in personas.yaml
    python -m testswarm run --agent retired-teacher --url https://staging.myapp.com
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from testswarm.config import (
    ConfigError,
    has_api_credentials,
    load_config,
    load_env,
    load_personas,
    load_secrets,
)
from testswarm.swarm import run_agents

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="TestSwarm: AI persona agents that test your web app in a real browser.")
console = Console()

CONFIG_OPT = typer.Option("config.yaml", "--config", help="Path to config.yaml")
PERSONAS_OPT = typer.Option("personas.yaml", "--personas", help="Path to personas.yaml")


def _fail(msg: str) -> None:
    console.print(f"[bold red]Error:[/] {msg}")
    raise typer.Exit(1)


@app.command("list")
def list_agents(personas: Path = PERSONAS_OPT) -> None:
    """Show the agents defined in personas.yaml."""
    try:
        pf = load_personas(personas)
    except ConfigError as e:
        _fail(str(e))
    t = Table(title=f"Agents in {personas}")
    t.add_column("name", style="bold cyan")
    t.add_column("persona")
    t.add_column("objective")
    t.add_column("start url")
    for a in pf.agents:
        t.add_row(a.name, a.persona, a.objective, pf.start_url(a))
    console.print(t)


@app.command()
def check(config: Path = CONFIG_OPT, personas: Path = PERSONAS_OPT) -> None:
    """Validate config.yaml, personas.yaml and .env without running anything."""
    load_env()
    try:
        cfg = load_config(config)
        pf = load_personas(personas)
    except ConfigError as e:
        _fail(str(e))
    console.print(f"[green]OK[/] config.yaml (model {cfg.model.name}, safe mode {cfg.guardrails.safe_mode})")
    console.print(f"[green]OK[/] personas.yaml ({len(pf.agents)} agents)")
    if has_api_credentials():
        console.print("[green]OK[/] Anthropic API key found")
    else:
        console.print("[yellow]![/] No ANTHROPIC_API_KEY found: copy .env.example to .env and add your key")
    secrets = load_secrets()
    console.print(f"[green]OK[/] login placeholders: {', '.join('{{' + s + '}}' for s in secrets) or 'none'}")


@app.command()
def run(
    agent: Optional[List[str]] = typer.Option(
        None, "--agent", "-a", help="Agent name from personas.yaml. Repeat for several: -a shopper -a teacher"),
    all_agents: bool = typer.Option(False, "--all", help="Run every agent in personas.yaml in parallel"),
    url: Optional[str] = typer.Option(None, "--url", help="Override the start URL (for all chosen agents)"),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="Don't start the live web dashboard"),
    config: Path = CONFIG_OPT,
    personas: Path = PERSONAS_OPT,
) -> None:
    """Run one or more agents against the website."""
    if not agent and not all_agents:
        _fail("Choose agents: --agent <name> (repeatable) or --all   (see: python -m testswarm list)")

    load_env()
    try:
        cfg = load_config(config)
        pf = load_personas(personas)
        names = [a.name for a in pf.agents] if all_agents else list(dict.fromkeys(agent or []))
        jobs = [(pf.get(n), pf.start_url(pf.get(n), url)) for n in names]
    except ConfigError as e:
        _fail(str(e))
    if not has_api_credentials():
        console.print("[yellow]Warning:[/] no ANTHROPIC_API_KEY in environment or .env; "
                      "the run will fail unless you're logged in with `ant auth login`.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = jobs[0][0].name if len(jobs) == 1 else "swarm"
    base_dir = Path(cfg.output.runs_dir) / f"{stamp}_{label}"
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        reports = asyncio.run(run_agents(cfg, jobs, base_dir, load_secrets(), console,
                                         dashboard=cfg.dashboard.enabled and not no_dashboard))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by you (Ctrl+C).[/] Partial logs are in " + str(base_dir))
        sys.exit(130)

    if len(jobs) == 1 and reports:
        console.print(f"[dim]Replay steps: playwright show-trace {base_dir / 'trace.zip'}[/]")


@app.command()
def demo(
    agents: int = typer.Option(10, "--agents", "-n", min=1, max=10, help="How many simulated agents (1-10)"),
    headless: bool = typer.Option(False, "--headless", help="Hide the browser windows (dashboard only)"),
    pause_timeout: int = typer.Option(120, "--pause-timeout", min=10, help="Seconds before an unanswered pause ends"),
    parallel: int = typer.Option(3, "--parallel", "-p", min=1, max=10,
                                 help="Browsers at the same time (others wait). Each needs ~300-500 MB RAM"),
    config: Path = CONFIG_OPT,
) -> None:
    """Simulate up to 10 agents on saucedemo.com with a scripted fake AI. NO API calls, no cost."""
    from testswarm.demo import run_demo
    try:
        cfg = load_config(config)
    except ConfigError as e:
        _fail(str(e))
    try:
        run_demo(cfg, agents, headless, pause_timeout, console, parallel)
    except KeyboardInterrupt:
        console.print("\n[yellow]Demo stopped (Ctrl+C).[/]")
        sys.exit(130)
