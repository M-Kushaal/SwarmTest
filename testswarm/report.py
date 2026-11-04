"""Final report: runs/<id>/report.md (for humans) + report.json (for the Phase 2 dashboard).

Facts (journey, blocked actions, technical errors) come straight from the logs.
Judgement (friction, recommendations, persona feedback) comes from one Claude call
that reads the whole log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testswarm.agent import RunResult
from testswarm.browser import TechIssues
from testswarm.guardrails import Guard
from testswarm.llm import LLM, LLMError

SEVERITY = {"type": "string", "enum": ["high", "medium", "low"]}

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective_achieved": {"type": "string", "enum": ["yes", "partially", "no"]},
        "usability_score": {"type": "integer", "description": "1 (unusable) to 10 (effortless) for THIS persona"},
        "summary": {"type": "string", "description": "3-5 sentence overview of the session"},
        "friction_points": {"type": "array", "items": {
            "type": "object",
            "properties": {"step": {"type": "integer"}, "severity": SEVERITY,
                           "issue": {"type": "string"}, "evidence": {"type": "string"}},
            "required": ["step", "severity", "issue", "evidence"], "additionalProperties": False}},
        "bugs": {"type": "array", "items": {
            "type": "object",
            "properties": {"severity": SEVERITY, "description": {"type": "string"},
                           "evidence": {"type": "string"}},
            "required": ["severity", "description", "evidence"], "additionalProperties": False}},
        "positives": {"type": "array", "items": {"type": "string"}},
        "persona_feedback": {"type": "string",
                             "description": "First-person feedback in the persona's own voice, 1-2 paragraphs"},
        "recommendations": {"type": "array", "items": {
            "type": "object",
            "properties": {"priority": SEVERITY, "recommendation": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["priority", "recommendation", "why"], "additionalProperties": False}},
    },
    "required": ["objective_achieved", "usability_score", "summary", "friction_points", "bugs",
                 "positives", "persona_feedback", "recommendations"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a senior UX researcher. You receive the complete log of one simulated user session "
    "(an AI agent role-playing a persona on a website). Write an honest, specific, evidence-based analysis. "
    "Cite step numbers. Distinguish real product problems from limitations of the simulation "
    "(e.g. actions blocked by the test's own safety rules are NOT product bugs). Don't invent issues "
    "that the log doesn't support."
)


def _session_text(r: RunResult, issues: TechIssues, guard: Guard) -> str:
    lines = [
        f"Website: {r.url}",
        f"Objective: {r.objective}",
        f"Persona profile: {json.dumps(r.profile, ensure_ascii=False)}",
        f"Run status: {r.status} after {len(r.steps)} steps, {r.seconds:.0f}s",
        f"Agent's final words: {r.final_text}",
        "",
        "STEP LOG:",
    ]
    for s in r.steps:
        lines.append(
            f"Step {s['step']} | {s['url']} | confusion {s['confusion']}/5 | {s['emotion']}\n"
            f"  saw: {s['observation']}\n  thought: {s['thought']}\n"
            f"  action: {s['action_desc']}\n  result: {s['result']}")
    lines += ["", "AGENT NOTES:"] + [f"- step {n['step']}: {n['note']}" for n in r.notes]
    lines += ["", "ACTIONS BLOCKED BY THE TEST'S SAFETY RULES (not product bugs):"]
    lines += [f"- {b.kind}: {b.target} ({b.reason})" + (" [then allowed once by the human supervisor]"
              if b.overridden else "") for b in guard.blocked] or ["- none"]
    lines += ["", "TECHNICAL ERRORS CAPTURED BY THE BROWSER:"]
    for title, items in _tech_sections(issues):
        lines.append(f"{title}: " + ("; ".join(items[:30]) if items else "none"))
    return guard.mask("\n".join(lines))


def _tech_sections(issues: TechIssues) -> list[tuple[str, list[str]]]:
    return [("JavaScript errors", issues.page_errors), ("Console errors", issues.console_errors),
            ("HTTP errors (4xx/5xx)", issues.http_errors), ("Failed requests", issues.failed_requests),
            ("Pop-up dialogs", issues.dialogs)]


async def write_report(llm: LLM, effort: str, run_dir: Path, r: RunResult,
                       issues: TechIssues, guard: Guard) -> Path:
    analysis: dict[str, Any] | None = None
    error = ""
    try:
        analysis = await llm.json_call(
            system=SYSTEM, content=[{"type": "text", "text": _session_text(r, issues, guard)}],
            schema=REPORT_SCHEMA, effort=effort, max_tokens=16000)
        analysis["usability_score"] = max(1, min(10, int(analysis.get("usability_score", 5))))
    except LLMError as e:
        error = str(e)

    data = {
        "agent": r.agent, "url": r.url, "objective": r.objective, "persona": r.profile,
        "status": r.status, "final_text": r.final_text, "steps_taken": len(r.steps),
        "seconds": round(r.seconds, 1), "tokens": r.tokens, "analysis": analysis,
        "analysis_error": error or None, "notes": r.notes,
        "blocked_actions": [b.__dict__ for b in guard.blocked],
        "technical_issues": issues.__dict__, "steps": r.steps,
    }
    (run_dir / "report.json").write_text(
        guard.mask(json.dumps(data, indent=2, ensure_ascii=False)), encoding="utf-8")

    md = guard.mask(_markdown(r, analysis, error, issues, guard))
    path = run_dir / "report.md"
    path.write_text(md, encoding="utf-8")
    return path


def _cell(text: str, n: int = 140) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= n else text[: n - 1] + "…"


def _markdown(r: RunResult, a: dict[str, Any] | None, error: str, issues: TechIssues, guard: Guard) -> str:
    p = r.profile
    who = p.get("display_name") or r.agent
    out = [f"# TestSwarm report: {r.agent}", ""]
    out += ["| | |", "|---|---|",
            f"| **Website** | {r.url} |",
            f"| **Persona** | {who}: {_cell(p.get('original_description', ''), 200)} |",
            f"| **Objective** | {_cell(r.objective, 200)} |",
            f"| **Run status** | {r.status} |"]
    if a:
        out += [f"| **Objective achieved** | {a['objective_achieved']} |",
                f"| **Usability score** | {a['usability_score']}/10 |"]
    avg = sum(s["confusion"] for s in r.steps) / len(r.steps) if r.steps else 0
    out += [f"| **Steps / time / tokens** | {len(r.steps)} / {r.seconds:.0f}s / {r.tokens:,} |",
            f"| **Average confusion** | {avg:.1f}/5 |", ""]

    if a:
        out += ["## Summary", "", a["summary"], ""]
        out += [f"## In {who}'s words", "", "> " + a["persona_feedback"].replace("\n", "\n> "), ""]
    else:
        out += ["## Summary", "", f"_AI analysis unavailable: {error}_", "",
                f"Agent's final words: {r.final_text}", ""]

    out += ["## Journey", "", "| Step | Page | Action | Result | Confusion | Thought |",
            "|---|---|---|---|---|---|"]
    for s in r.steps:
        out.append(f"| [{s['step']}]({s['screenshot']}) | {_cell(s['title'] or s['url'], 50)} | "
                   f"{_cell(s['action_desc'], 60)} | {_cell(s['result'], 50)} | {s['confusion']}/5 | "
                   f"{_cell(s['thought'], 120)} |")
    out.append("")

    if a:
        out += ["## Friction points", ""]
        out += [f"- **[{f['severity']}] step {f['step']}:** {f['issue']} _(evidence: {f['evidence']})_"
                for f in sorted(a["friction_points"], key=lambda f: ["high", "medium", "low"].index(f["severity"]))
                ] or ["- none found"]
        out += ["", "## Bugs", ""]
        out += [f"- **[{b['severity']}]** {b['description']} _(evidence: {b['evidence']})_"
                for b in a["bugs"]] or ["- none found"]
        out += ["", "## What worked well", ""] + ([f"- {x}" for x in a["positives"]] or ["- nothing noted"])
        out += ["", "## Recommendations", ""]
        out += [f"- **[{x['priority']}]** {x['recommendation']}: {x['why']}"
                for x in sorted(a["recommendations"],
                                key=lambda x: ["high", "medium", "low"].index(x["priority"]))] or ["- none"]
        out.append("")

    if r.notes:
        out += ["## Agent's notes during the session", ""] + [f"- step {n['step']}: {n['note']}" for n in r.notes] + [""]

    out += ["## Technical errors captured by the browser", ""]
    for title, items in _tech_sections(issues):
        out.append(f"**{title}:** " + ("none" if not items else ""))
        out += [f"- `{_cell(i, 200)}`" for i in items[:25]]
    out.append("")

    out += ["## Actions blocked by guardrails", ""]
    out += [f"- {b.time} **{b.kind}**: {_cell(b.target, 120)}: {b.reason}"
            + (" **(allowed once by you)**" if b.overridden else "") for b in guard.blocked] or ["- none"]
    out += ["", "---", "", "Replay every step: `playwright show-trace trace.zip` (run inside this folder).",
            "Full step data: `steps.jsonl` and `report.json`. Screenshots: `screenshots/`.", ""]
    return "\n".join(out)
