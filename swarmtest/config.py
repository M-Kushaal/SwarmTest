"""Load and validate config.yaml, personas.yaml and .env secrets.

Everything is checked up front so mistakes produce a clear message like:
    config.yaml -> guardrails.safe_mode: Input should be 'normal' or 'strict'
instead of a crash in the middle of a run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SECRET_PREFIX = "SWARMTEST_SECRET_"
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ConfigError(Exception):
    """A friendly, user-facing configuration problem."""


class _Strict(BaseModel):
    # extra="forbid" -> an unknown key (usually a typo) is an error, not silently ignored
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- config.yaml

class ModelSettings(_Strict):
    name: str = "claude-opus-5-5"
    step_effort: Effort = "medium"
    report_effort: Effort = "high"
    screenshots_in_context: int = Field(2, ge=1, le=10)


class Limits(_Strict):
    max_steps: int = Field(30, ge=1, le=500)
    max_minutes: float = Field(10, gt=0)
    max_total_tokens: int = Field(600_000, ge=20_000)


class Viewport(_Strict):
    width: int = Field(1280, ge=320)
    height: int = Field(800, ge=320)


class BrowserSettings(_Strict):
    headless: bool = False
    viewport: Viewport = Viewport()
    slow_mo_ms: int = Field(150, ge=0)
    highlight_before_click_ms: int = Field(600, ge=0)
    page_load_timeout_s: int = Field(20, ge=1)
    save_trace: bool = True


def _clean_domain(d: str) -> str:
    """'https://Auth.MyApp.com/login' -> 'auth.myapp.com'"""
    d = d.strip().lower()
    if "://" in d:
        d = urlparse(d).hostname or ""
    return d.split("/")[0].lstrip(".")


class Guardrails(_Strict):
    extra_allowed_domains: list[str] = []
    safe_mode: Literal["normal", "strict"] = "normal"
    blocked_payment_domains: list[str] = []
    blocked_click_words: list[str] = []
    blocked_field_keywords: list[str] = []
    allow_downloads: bool = False
    allow_file_uploads: bool = False

    @field_validator("extra_allowed_domains", "blocked_payment_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [d for d in (_clean_domain(x) for x in v) if d]

    @field_validator("blocked_click_words", "blocked_field_keywords")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        return [w.strip().lower() for w in v if w and w.strip()]


class CaptchaSettings(_Strict):
    pause_for_human: bool = True


class PauseSettings(_Strict):
    on_block: Literal["pause", "continue"] = "pause"
    timeout_s: int = Field(300, ge=10)


class SwarmSettings(_Strict):
    max_parallel: int = Field(5, ge=1, le=50)


class DashboardSettings(_Strict):
    enabled: bool = True
    port: int = Field(8765, ge=1024, le=65535)
    open_browser: bool = True


class OutputSettings(_Strict):
    runs_dir: str = "runs"


class Config(_Strict):
    model: ModelSettings = ModelSettings()
    limits: Limits = Limits()
    browser: BrowserSettings = BrowserSettings()
    guardrails: Guardrails = Guardrails()
    captcha: CaptchaSettings = CaptchaSettings()
    pauses: PauseSettings = PauseSettings()
    swarm: SwarmSettings = SwarmSettings()
    dashboard: DashboardSettings = DashboardSettings()
    output: OutputSettings = OutputSettings()


# ------------------------------------------------------------- personas.yaml

def _check_url(u: str) -> str:
    u = u.strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"'{u}' is not a valid URL (must start with http:// or https://)")
    return u


class AgentSpec(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$",
                      description="lowercase letters, numbers, - or _ ; no spaces")
    persona: str = Field(min_length=10)
    objective: str = Field(min_length=5)
    url: str | None = None

    @field_validator("persona", "objective")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())  # collapse YAML line breaks into one clean line

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v) if v else None


class PersonasFile(_Strict):
    url: str | None = None
    agents: list[AgentSpec] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v) if v else None

    @model_validator(mode="after")
    def _checks(self) -> "PersonasFile":
        names = [a.name for a in self.agents]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate agent names: {', '.join(dupes)}")
        missing = [a.name for a in self.agents if not (a.url or self.url)]
        if missing:
            raise ValueError(f"no url for agent(s) {', '.join(missing)}: "
                             "set a top-level `url:` or a per-agent `url:`")
        return self

    def get(self, name: str) -> AgentSpec:
        for a in self.agents:
            if a.name == name:
                return a
        available = ", ".join(a.name for a in self.agents)
        raise ConfigError(f"No agent named '{name}' in personas.yaml. Available: {available}")

    def start_url(self, agent: AgentSpec, override: str | None = None) -> str:
        """Priority: --url flag > agent's own url > top-level url."""
        if override:
            try:
                return _check_url(override)
            except ValueError as e:
                raise ConfigError(str(e)) from None
        return agent.url or self.url  # type: ignore[return-value]


# ------------------------------------------------------------------- loaders

def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Can't find {path}. Are you running from the SwarmTest folder?")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML:\n{e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should contain key: value settings at the top level.")
    return data


def _friendly(path: Path, err: ValidationError) -> ConfigError:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(top level)"
        lines.append(f"  {path.name} -> {where}: {e['msg']}")
    return ConfigError("Problem(s) found:\n" + "\n".join(lines))


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    try:
        return Config.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise _friendly(path, e) from None


def load_personas(path: str | Path = "personas.yaml") -> PersonasFile:
    path = Path(path)
    try:
        return PersonasFile.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise _friendly(path, e) from None


# ------------------------------------------------------------------- secrets

def load_env() -> None:
    """Load .env into environment variables (does not override ones already set)."""
    load_dotenv(override=False)


def has_api_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def load_secrets() -> dict[str, str]:
    """SWARMTEST_SECRET_PASSWORD=abc  ->  {"PASSWORD": "abc"}
    The agent only ever sees "{{PASSWORD}}"; values stay in this process."""
    return {
        key[len(SECRET_PREFIX):]: value
        for key, value in os.environ.items()
        if key.startswith(SECRET_PREFIX) and value
    }
