"""Runs one or many agents at the same time and writes a combined summary.

Single agent:  runs/<time>_<agent>/...
Several:       runs/<time>_swarm/<agent>/...  +  runs/<time>_swarm/summary.md
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rich.console import Console

from swarmtest.agent import Agent
from swarmtest.config import AgentSpec, Config
from swarmtest.dashboard import Dashboard
from swarmtest.report import write_report
from swarmtest.supervisor import Supervisor

STAGGER_S = 1.5  # seconds between agent starts


async def run_agents(cfg: Config, jobs: list[tuple[AgentSpec, str]], base_dir: Path,
                     secrets: dict[str, str], console: Console, *, dashboard: bool) -> list[Path]:
    """jobs = [(agent spec, start url), ...]. Returns the report paths."""
    multi = len(jobs) > 1
    supervisor = Supervisor(console, cfg.pauses.timeout_s)
    dash: Dashboard | None = None
    if dashboard:
        dash = Dashboard(supervisor, cfg.dashboard.port)
        dash.state.set_meta(base_dir.name)
        for spec, url in jobs:
            dash.state.register(spec.name, spec.persona, spec.objective, url, cfg.limits.max_steps)
        try:
            url = dash.start(cfg.dashboard.open_browser)
            console.print(f"[bold cyan]Live dashboard:[/] {url}")
        except RuntimeError as e:
            console.print(f"[yellow]{e} - continuing without the dashboard.[/]")
            dash = None

    slots = asyncio.Semaphore(cfg.swarm.max_parallel)
    if multi:
        console.print(f"[bold]Starting {len(jobs)} agents[/] (at most {cfg.swarm.max_parallel} at a time). "
                      f"Results: {base_dir}")

    async def one(index: int, spec: AgentSpec, url: str) -> Path | None:
        # stagger starts: launching many browsers at the same instant overloads a laptop
        await asyncio.sleep(index * STAGGER_S)
        async with slots:
            run_dir = base_dir / spec.name if multi else base_dir
            run_dir.mkdir(parents=True, exist_ok=True)
            agent = Agent(cfg, spec, url, run_dir, secrets, console, supervisor=supervisor,
                          listeners=[dash.state.on_event] if dash else [], index=index, multi=multi)
            result = await agent.run()
            try:
                report = await write_report(agent.llm, cfg.model.report_effort, run_dir, result,
                                            agent.browser.issues, agent.guard)
            except Exception as e:  # one agent's report failing must not affect the others
                agent.log.warning(f"Report failed: {type(e).__name__}: {e}")
                return None
            agent.log.report_written(report)
            return report

    try:
        results = await asyncio.gather(*(one(i, s, u) for i, (s, u) in enumerate(jobs)))
        reports = [r for r in results if r]
        if multi:
            summary = write_summary(base_dir, [s.name for s, _ in jobs])
            if dash:
                dash.state.set_summary(summary)
            console.print(f"\n[bold green]Swarm summary:[/] {summary}")
        if dash:
            dash.state.finished = True
            console.print("[dim]The dashboard stays available until you close this program "
                          "(press Enter to exit).[/]")
            await supervisor.wait_for_enter()
        return reports
    finally:
        if dash:
            dash.stop()


# ------------------------------------------------------------------- summary

STATUS_ORDER = ["completed", "gave_up", "ended_at_block", "ended_by_user", "max_steps", "timeout",
                "token_limit", "captcha_unsolved", "stuck", "error"]


def _load(run_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_summary(base_dir: Path, names: list[str]) -> Path:
    rows, sections = [], []
    totals = {"steps": 0, "tokens": 0, "blocked": 0}
    for name in names:
        d = _load(base_dir / name)
        if not d:
            rows.append(f"| {name} | no report | | | | | | |")
            continue
        a = d.get("analysis") or {}
        steps = d.get("steps") or []
        avg = sum(s["confusion"] for s in steps) / len(steps) if steps else 0
        blocked = len(d.get("blocked_actions") or [])
        totals["steps"] += len(steps)
        totals["tokens"] += d.get("tokens", 0)
        totals["blocked"] += blocked
        rows.append(f"| [{name}]({name}/report.md) | {d.get('status')} | {a.get('objective_achieved', '–')} | "
                    f"{a.get('usability_score', '–')}{'/10' if a.get('usability_score') else ''} | {len(steps)} | "
                    f"{avg:.1f}/5 | {blocked} | {d.get('tokens', 0):,} |")
        high = [f for f in a.get("friction_points", []) if f.get("severity") == "high"]
        recs = [r for r in a.get("recommendations", []) if r.get("priority") == "high"]
        if high or recs or a.get("summary"):
            sections += [f"### {name}", "", a.get("summary", ""), ""]
            sections += [f"- **Friction (high), step {f['step']}:** {f['issue']}" for f in high]
            sections += [f"- **Fix (high):** {r['recommendation']}" for r in recs]
            sections.append("")

    md = ["# SwarmTest: swarm summary", "",
          f"{len(names)} agents · {totals['steps']} steps · {totals['blocked']} blocked actions · "
          f"{totals['tokens']:,} tokens", "",
          "| Agent | Status | Objective achieved | Usability | Steps | Avg confusion | Blocked | Tokens |",
          "|---|---|---|---|---|---|---|---|", *rows, "",
          "## Highlights per agent", "", *(sections or ["_No AI analysis available._"]), ""]
    path = base_dir / "summary.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path
