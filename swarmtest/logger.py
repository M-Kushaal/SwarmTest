"""Traceability: live terminal view + permanent step log (runs/<id>/steps.jsonl).

Every record passes through Guard.mask() so real secret values never appear.
`listeners` receive every event (the live dashboard subscribes here).
With several agents, each terminal line is prefixed with the agent's name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swarmtest.guardrails import Guard

CONFUSION_COLORS = {1: "green", 2: "green", 3: "yellow", 4: "dark_orange", 5: "red"}
AGENT_COLORS = ["cyan", "magenta", "bright_blue", "yellow", "green", "bright_red",
                "bright_cyan", "bright_magenta", "blue", "bright_green"]
FINISH_STYLES = {"completed": "green", "gave_up": "yellow", "ended_by_user": "yellow",
                 "ended_at_block": "yellow", "captcha_unsolved": "yellow"}


class RunLogger:
    def __init__(self, run_dir: Path, agent_name: str, guard: Guard, console: Console | None = None,
                 prefix: bool = False, color_index: int = 0):
        self.run_dir = run_dir
        self.agent_name = agent_name
        self.guard = guard
        self.console = console or Console()
        self.steps_file = run_dir / "steps.jsonl"
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        color = AGENT_COLORS[color_index % len(AGENT_COLORS)]
        self.tag = Text(f"[{agent_name}] ", style=f"bold {color}") if prefix else Text("")

    # ---------------------------------------------------------------- output
    def _emit(self, event: dict[str, Any], save: bool = True) -> None:
        event = json.loads(self.guard.mask(json.dumps(event, ensure_ascii=False)))
        event["agent"] = self.agent_name
        if save:
            with self.steps_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        for listener in self.listeners:
            try:
                listener(event)
            except Exception:  # a broken listener must never stop the agent
                pass

    def _line(self, *parts: Text | str) -> None:
        self.console.print(Text.assemble(self.tag, *parts))

    def _title(self, title: str) -> str:
        return escape(f"[{self.agent_name}] {title}" if self.tag.plain else title)

    # ---------------------------------------------------------------- events
    def run_started(self, *, url: str, persona: str, objective: str, model: str, safe_mode: str,
                    secrets: list[str], max_steps: int, viewport: list[int] | None = None) -> None:
        self._emit({"event": "run_started", "url": url, "persona": persona, "objective": objective,
                    "model": model, "safe_mode": safe_mode, "max_steps": max_steps, "viewport": viewport,
                    "run_dir": str(self.run_dir)})
        t = Table.grid(padding=(0, 2))
        t.add_row("[bold]Agent[/]", escape(self.agent_name))
        t.add_row("[bold]URL[/]", escape(url))
        t.add_row("[bold]Persona[/]", escape(persona))
        t.add_row("[bold]Objective[/]", escape(objective))
        t.add_row("[bold]Model[/]", model)
        t.add_row("[bold]Safe mode[/]", safe_mode)
        t.add_row("[bold]Login placeholders[/]", ", ".join("{{" + s + "}}" for s in secrets) or "none")
        t.add_row("[bold]Run folder[/]", escape(str(self.run_dir)))
        self.console.print(Panel(t, title=self._title("SwarmTest agent starting"), border_style="cyan"))

    def phase(self, phase: str, step: int, **extra: Any) -> None:
        """Live status for the dashboard only (not saved, not printed): observing / thinking / acting."""
        self._emit({"event": "phase", "phase": phase, "step": step, **extra}, save=False)

    def info(self, message: str) -> None:
        self._emit({"event": "info", "message": message})
        self._line(Text("> ", style="cyan"), self.guard.mask(message))

    def warning(self, message: str) -> None:
        self._emit({"event": "warning", "message": message})
        self._line(Text("! " + self.guard.mask(message), style="yellow"))

    def step(self, record: dict[str, Any]) -> None:
        self._emit({"event": "step", **record})
        c = record.get("confusion", 1)
        self._line(
            (f"Step {record['step']:>2}", "bold"), "  ",
            (f"confusion {c}/5", CONFUSION_COLORS.get(c, "white")), "  ",
            (record.get("emotion", ""), "magenta"), "  ",
            (self.guard.mask(record.get("url", ""))[:90], "dim"),
        )
        self._line(Text("   thinks: ", style="dim"), Text(self.guard.mask(record.get("thought", "")), style="italic"))
        action_style = "bold red" if record.get("blocked") else "bold"
        self._line(Text("   action: ", style="dim"), Text(self.guard.mask(record.get("action_desc", "")), style=action_style))
        result_style = "red" if record.get("blocked") or record.get("failed") else "green"
        self._line(Text("   result: ", style="dim"), Text(self.guard.mask(record.get("result", "")), style=result_style))

    def run_finished(self, *, status: str, summary: str, steps: int, seconds: float, tokens: int) -> None:
        self._emit({"event": "run_finished", "status": status, "summary": summary, "steps": steps,
                    "seconds": round(seconds, 1), "tokens": tokens})
        style = FINISH_STYLES.get(status, "red")
        self.console.print(Panel(
            f"[bold]Status:[/] [{style}]{status}[/]\n[bold]Steps:[/] {steps}   "
            f"[bold]Time:[/] {seconds:.0f}s   [bold]Tokens:[/] {tokens:,}\n\n{escape(self.guard.mask(summary))}",
            title=self._title("Run finished"), border_style=style))

    def report_written(self, path: Path) -> None:
        self._emit({"event": "report_written", "path": str(path)})
        self._line(Text("Report: ", style="bold green"), str(path))
