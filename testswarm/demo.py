"""Demo mode: simulate up to 10 agents on saucedemo.com with a SCRIPTED fake AI.

NO Claude API calls, no credits. Everything else is real: real browsers, real clicks,
real guardrails, real pauses, real dashboard, real reports (with fake analysis).

    python -m testswarm demo                 # 10 agents
    python -m testswarm demo --agents 3
    python -m testswarm demo --headless      # no browser windows, just the dashboard
    python -m testswarm demo --parallel 3    # at most 3 browsers at once (less RAM)
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from testswarm.agent import DECISION_SCHEMA
from testswarm.config import AgentSpec, Config
from testswarm.llm import LLM
from testswarm.persona import PROFILE_SCHEMA
from testswarm.report import REPORT_SCHEMA
from testswarm.swarm import run_agents

DEMO_URL = "https://www.saucedemo.com"
DEMO_SECRETS = {"USERNAME": "standard_user", "PASSWORD": "secret_sauce"}  # public demo login shown on the site

LOGIN = [("type", r'"Username"', "{{USERNAME}}"), ("type", r'"Password"', "{{PASSWORD}}!enter")]

# (action, regex on the element line / url / text). "!enter" = press Enter after typing.
SCRIPTS: dict[str, list[tuple[str, str, str | None]]] = {
    "cheapest": LOGIN + [("type", r"Price \(low to high\)", "Price (low to high)"), ("click", r'"Add to cart"', None),
                         ("navigate", "/cart.html", None), ("click", r'"Checkout"', None),
                         ("finish", "", "Found the cheapest item and reached checkout.")],
    "browser": LOGIN + [("scroll", "down", None), ("scroll", "down", None), ("click", r'"Sauce Labs Fleece Jacket"', None),
                        ("note", "", "Product page has no reviews or size guide."), ("go_back", "", None),
                        ("finish", "", "Browsed the catalogue; product pages are thin on detail.")],
    "explorer": LOGIN + [("click", r'"Open Menu"', None), ("click", r'"About"', None),   # leaves site -> hard block
                         ("finish", "", "Explored the menu; About goes to another website.")],
    "sorter": LOGIN + [("type", r"Name \(Z to A\)", "Name (Z to A)"), ("type", r"Price \(high to low\)", "Price (high to low)"),
                       ("click", r'"Add to cart"', None), ("navigate", "/cart.html", None),
                       ("click", r'"Continue Shopping"', None), ("finish", "", "Sorting works well.")],
    "impatient": LOGIN + [("scroll", "down", None), ("give_up", "", "Too many products and no search box. I'm out.")],
    "first-login": [("type", r'"Username"', "{{USERNAME}}"), ("type", r'"Password"', "{{PASSWORD}}!enter"),
                   ("note", "", "Login error messages could be friendlier."), ("finish", "", "Logged in fine.")],
    "cart-fan": LOGIN + [("click", r'"Add to cart"', None), ("click", r'"Add to cart"', None),
                         ("click", r'"Add to cart"', None), ("navigate", "/cart.html", None),
                         ("click", r'"Remove"', None), ("click", r'"Checkout"', None),
                         ("finish", "", "Cart add/remove works; checkout needed approval.")],
    "detail": LOGIN + [("click", r'"Sauce Labs Bike Light"', None), ("click", r'"Add to cart"', None),
                       ("click", r'"Back to products"', None), ("finish", "", "Product detail flow is clear.")],
    "offsite": LOGIN + [("navigate", "https://google.com", None),   # hard block -> ask you
                        ("finish", "", "Stayed on the site.")],
    "footer": LOGIN + [("scroll", "down", None), ("scroll", "down", None), ("scroll", "down", None),
                       ("note", "", "Footer social links leave the site."), ("finish", "", "Reached the footer.")],
}

PERSONAS = [
    ("lunch-shopper", "30 year old office worker on a lunch break, impatient", "cheapest"),
    ("careful-retiree", "68 year old retiree who reads every word slowly", "browser"),
    ("curious-student", "21 year old student who clicks on everything", "explorer"),
    ("bargain-hunter", "40 year old who always compares prices", "sorter"),
    ("busy-founder", "34 year old founder with no patience at all", "impatient"),
    ("first-timer", "55 year old using an online shop for the first time", "first-login"),
    ("gift-buyer", "28 year old buying several gifts at once", "cart-fan"),
    ("detail-reader", "45 year old engineer who wants every spec", "detail"),
    ("wanderer", "19 year old who gets distracted easily", "offsite"),
    ("scroller", "37 year old who scrolls to the bottom of every page", "footer"),
]

THOUGHTS = ["Okay, let's see what this is.", "Hmm, where do I go next?", "That looks simple enough.",
            "Not sure this is what I wanted.", "Right, moving on.", "This is taking a while.",
            "Oh nice, that was easy.", "Wait, what does that button do?"]
EMOTIONS = ["neutral", "curious", "happy", "confused", "frustrated", "bored", "impressed"]


class FakeBrain:
    """Plays each agent from its script by reading the same element list the real AI gets."""

    def __init__(self) -> None:
        self.pos: dict[str, int] = {}
        self.rng = random.Random(7)

    async def __call__(self, llm: LLM, *, system: str, content: list[dict[str, Any]], schema: dict[str, Any],
                       effort: str, max_tokens: int = 16000) -> dict[str, Any]:
        llm.total_tokens += self.rng.randint(4000, 7000)
        await asyncio.sleep(self.rng.uniform(0.8, 2.2))   # "thinking" time, so it looks realistic
        if schema is PROFILE_SCHEMA:
            return {"display_name": "Demo persona", "age": "–", "occupation": "–", "background": "Simulated.",
                    "tech_comfort": "medium", "patience": "medium", "reading_style": "skims", "goals": [],
                    "worries": [], "habits": [], "gives_up_when": "–", "inner_voice": "plain"}
        if schema is REPORT_SCHEMA:
            return {"objective_achieved": self.rng.choice(["yes", "partially", "no"]),
                    "usability_score": self.rng.randint(4, 9),
                    "summary": "DEMO MODE: scripted agent, not a real AI analysis.",
                    "friction_points": [{"step": 1, "severity": "medium", "issue": "Demo friction point",
                                         "evidence": "scripted"}],
                    "bugs": [], "positives": ["Demo positive"], "persona_feedback": "This was a simulated session.",
                    "recommendations": [{"priority": "low", "recommendation": "Run a real test with Claude",
                                         "why": "Demo mode uses a scripted fake AI."}]}
        assert schema is DECISION_SCHEMA
        script_name = re.search(r"\(demo script: ([\w-]+)\)", system).group(1)
        script = SCRIPTS[script_name]
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        i = self.pos.get(script_name, 0)
        kind, target, arg = script[min(i, len(script) - 1)]
        self.pos[script_name] = i + 1
        action: dict[str, Any] = {"type": kind, "element_id": None, "text": None, "press_enter": None,
                                  "direction": None, "url": None, "seconds": None}
        if kind in ("click", "type"):
            line = next((ln for ln in text.splitlines() if ln.startswith("[") and re.search(target, ln)), None)
            if line is None:  # not on screen: scroll instead, as a person would
                action = {**action, "type": "scroll", "direction": "down"}
            else:
                action["element_id"] = int(line[1:line.index("]")])
                if kind == "type":
                    enter = bool(arg and arg.endswith("!enter"))
                    action["text"] = (arg or "").replace("!enter", "")
                    action["press_enter"] = enter
        elif kind == "navigate":
            action["url"] = target
        elif kind == "scroll":
            action["direction"] = target
        elif kind in ("note", "finish", "give_up"):
            action["text"] = arg
        conf = self.rng.choice([1, 1, 2, 2, 3, 4]) if kind != "give_up" else 5
        return {"observation": "Simulated observation.", "thought": self.rng.choice(THOUGHTS), "confusion": conf,
                "emotion": "frustrated" if kind == "give_up" else self.rng.choice(EMOTIONS),
                "progress": f"demo step {i + 1}", "action": action}


def run_demo(cfg: Config, n: int, headless: bool, pause_timeout: int, console: Console,
             parallel: int | None = None) -> None:
    brain = FakeBrain()

    async def fake_json_call(self: LLM, **kwargs: Any) -> dict[str, Any]:
        return await brain(self, **kwargs)

    LLM.json_call = fake_json_call  # type: ignore[method-assign]  # demo only: never touches the API
    os.environ.setdefault("ANTHROPIC_API_KEY", "demo-mode-not-used")  # client is created but never called

    cfg.browser.headless = headless or cfg.browser.headless
    cfg.pauses.timeout_s = pause_timeout
    if parallel:
        cfg.swarm.max_parallel = parallel  # how many browsers at once; the rest wait their turn
    cfg.limits.max_steps = 12
    jobs = [(AgentSpec(name=name, persona=persona, objective=f"Shop on the demo store (demo script: {script})"),
             DEMO_URL) for name, persona, script in PERSONAS[:n]]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_dir = Path(cfg.output.runs_dir) / f"{stamp}_demo"
    base_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold magenta]DEMO MODE[/] - {n} simulated agents ({cfg.swarm.max_parallel} at a time), "
                  "scripted fake AI, [bold]no API calls[/].")
    console.print("Some agents will hit guardrails and pause: answer them on the dashboard or here "
                  f"(they auto-end after {pause_timeout}s).")
    asyncio.run(run_agents(cfg, jobs, base_dir, DEMO_SECRETS, console, dashboard=cfg.dashboard.enabled))
