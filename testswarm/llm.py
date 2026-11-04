"""Thin wrapper around the Claude API.

Every call is a single, self-contained request that returns JSON matching a
schema (structured outputs), so the agent always gets a well-formed decision.
Also tracks token usage for the run's token limit.

Recoverable API problems (credits ran out, rate limit, Claude overloaded, no
internet) do NOT end the run: `on_pause` is called, which asks the person to
fix it and press Enter (retry the exact same request, nothing lost) or quit.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

import anthropic

# Models that support server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# on_pause(kind, message) -> True = retry the same request, False = end the run
PauseHandler = Callable[[str, str], Awaitable[bool]]

PAUSE_HELP = {
    "billing": "Your Anthropic credit balance ran out. Top up at https://console.anthropic.com/settings/billing",
    "rate_limit": "Rate limit hit (too many requests). Wait a minute.",
    "overloaded": "Claude's servers are busy or had an error. Wait a minute.",
    "connection": "Can't reach the Claude API. Check your internet connection.",
}


class LLMError(Exception):
    """The model call failed in a way the run can't recover from."""


class LLMStopped(LLMError):
    """The person chose to end the run while it was paused on an API problem."""


def pause_kind(e: Exception) -> str | None:
    """Classify an API error as recoverable-by-waiting/topping-up, or None if it's fatal."""
    status = getattr(e, "status_code", None)
    msg = (getattr(e, "message", "") or str(e)).lower()
    if status == 402 or "credit balance" in msg or "billing" in msg or (
            "insufficient" in msg and ("credit" in msg or "fund" in msg)):
        return "billing"
    if isinstance(e, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(e, anthropic.InternalServerError) or status in (500, 502, 503, 504, 529) or "overloaded" in msg:
        return "overloaded"
    if isinstance(e, anthropic.APIConnectionError):  # includes timeouts
        return "connection"
    return None


class LLM:
    def __init__(self, model: str, on_pause: PauseHandler | None = None):
        self.model = model
        self.client = anthropic.AsyncAnthropic(max_retries=4)
        self.use_fallbacks = model in FALLBACK_MODELS
        self.total_tokens = 0
        self.on_pause = on_pause
        self.paused_seconds = 0.0  # time spent waiting for the person; excluded from the time limit

    async def json_call(self, *, system: str, content: list[dict[str, Any]], schema: dict[str, Any],
                        effort: str, max_tokens: int = 16000) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        response = await self._create_with_pause(kwargs)

        u = response.usage
        self.total_tokens += (u.input_tokens + u.output_tokens
                              + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0))

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request (safety refusal).")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off (max_tokens).")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("The model returned no answer.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"The model returned invalid JSON: {e}")

    async def _create_with_pause(self, kwargs: dict[str, Any]):
        while True:
            try:
                return await self._create(kwargs)
            except anthropic.APIError as e:
                kind = pause_kind(e)
                detail = getattr(e, "message", "") or str(e)
                if kind is None:
                    raise _fatal(e, self.model) from None
                if self.on_pause is None:
                    raise LLMError(f"{PAUSE_HELP[kind]} (API said: {detail})") from None
                started = time.monotonic()
                retry = await self.on_pause(kind, f"{PAUSE_HELP[kind]}\nAPI said: {detail}")
                self.paused_seconds += time.monotonic() - started
                if not retry:
                    raise LLMStopped(f"Run ended by you while paused: {PAUSE_HELP[kind]}") from None
                # loop: retry the exact same request (same context, same browser page)

    async def _create(self, kwargs: dict[str, Any]):
        if self.use_fallbacks:
            try:
                # If Claude's safety classifier declines, the API retries on a recommended fallback model.
                return await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                self.use_fallbacks = False  # not available for this account/model: use plain requests
        return await self.client.messages.create(**kwargs)


def _fatal(e: Exception, model: str) -> LLMError:
    if isinstance(e, anthropic.AuthenticationError):
        return LLMError("Anthropic API key is missing or invalid. Put ANTHROPIC_API_KEY in your .env file.")
    if isinstance(e, anthropic.NotFoundError):
        return LLMError(f"Model '{model}' not found. Check model.name in config.yaml.")
    if isinstance(e, anthropic.APIStatusError):
        return LLMError(f"Claude API error {e.status_code}: {e.message}")
    return LLMError(f"Claude API error: {e}")
