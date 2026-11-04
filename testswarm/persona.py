"""Turns the short persona text from personas.yaml into a full, consistent profile.

The profile is saved to runs/<id>/persona.yaml so you can see exactly who the agent was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from testswarm.config import AgentSpec
from testswarm.llm import LLM, LLMError, LLMStopped

LEVELS = ["very low", "low", "medium", "high", "very high"]

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "display_name": {"type": "string", "description": "A realistic first name + short label"},
        "age": {"type": "string"},
        "occupation": {"type": "string"},
        "background": {"type": "string", "description": "2-3 sentences of life context"},
        "tech_comfort": {"type": "string", "enum": LEVELS},
        "patience": {"type": "string", "enum": LEVELS},
        "reading_style": {"type": "string", "description": "How they read pages (skims, reads all, ...)"},
        "goals": {"type": "array", "items": {"type": "string"}},
        "worries": {"type": "array", "items": {"type": "string"}},
        "habits": {"type": "array", "items": {"type": "string"}, "description": "Browsing habits"},
        "gives_up_when": {"type": "string"},
        "inner_voice": {"type": "string", "description": "How their thoughts sound, tone and vocabulary"},
    },
    "required": ["display_name", "age", "occupation", "background", "tech_comfort", "patience",
                 "reading_style", "goals", "worries", "habits", "gives_up_when", "inner_voice"],
    "additionalProperties": False,
}

SYSTEM = (
    "You create realistic user personas for usability testing. Expand the short description into a "
    "concrete, believable person. Stay faithful to every detail given; only fill in what's missing. "
    "Keep it ordinary and realistic, not a caricature."
)


async def expand_persona(llm: LLM, spec: AgentSpec, effort: str) -> dict[str, Any]:
    content = [{"type": "text", "text": (
        f"Short persona description:\n{spec.persona}\n\n"
        f"(For context, this person will try to: {spec.objective})"
    )}]
    try:
        profile = await llm.json_call(system=SYSTEM, content=content, schema=PROFILE_SCHEMA,
                                      effort=effort, max_tokens=8000)
    except LLMStopped:
        raise  # the person chose to end the run
    except LLMError:
        profile = {}  # fall back to the raw description; the run can still go ahead
    return {"original_description": spec.persona, **profile}


def profile_to_text(profile: dict[str, Any]) -> str:
    """Human-readable block used inside the agent's system prompt."""
    if len(profile) <= 1:
        return profile["original_description"]
    lines = [f"Description: {profile['original_description']}"]
    for key, value in profile.items():
        if key == "original_description":
            continue
        label = key.replace("_", " ").capitalize()
        if isinstance(value, list):
            value = "; ".join(value)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def save_profile(run_dir: Path, spec: AgentSpec, profile: dict[str, Any]) -> None:
    data = {"agent": spec.name, "objective": spec.objective, "profile": profile}
    (run_dir / "persona.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
