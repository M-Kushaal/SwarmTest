"""Safety layer. Every network request, click and keystroke passes through here.

The AI is told the rules too, but it is NEVER trusted to follow them:
this code is what actually enforces them.

Each check returns None (allowed) or a short human-readable reason (blocked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from swarmtest.config import Guardrails

# Requests that only read data. In strict mode everything else is blocked.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
DIGIT_RUN_RE = re.compile(r"(?:\d[ -]?){13,19}")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _matches_domain(host: str, domain: str) -> bool:
    """'api.stripe.com' matches 'stripe.com'; 'notstripe.com' does not."""
    return host == domain or host.endswith("." + domain)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def looks_like_card_number(text: str) -> bool:
    for m in DIGIT_RUN_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


@dataclass
class BlockedEvent:
    kind: str        # "request" | "click" | "type" | "navigate" | "popup"
    target: str      # what it tried to touch
    reason: str
    overridable: bool = False    # True only for blocked-word clicks; a human may "allow" those once
    overridden: bool = False     # set when the human allowed it
    time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class Guard:
    def __init__(self, cfg: Guardrails, start_url: str, secrets: dict[str, str]):
        self.cfg = cfg
        self.secrets = secrets
        start_host = _host(start_url)
        base = start_host[4:] if start_host.startswith("www.") else start_host
        self.allowed_domains = [base, *cfg.extra_allowed_domains]
        self.click_patterns = [
            (w, re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE))
            for w in cfg.blocked_click_words
        ]
        self.blocked: list[BlockedEvent] = []

    # ------------------------------------------------------------ bookkeeping
    def _block(self, kind: str, target: str, reason: str, overridable: bool = False) -> str:
        self.blocked.append(BlockedEvent(kind, self.mask(target)[:200], reason, overridable))
        return reason

    @property
    def last_block(self) -> BlockedEvent | None:
        return self.blocked[-1] if self.blocked else None

    # ---------------------------------------------------------------- domains
    def is_allowed_host(self, host: str) -> bool:
        return any(_matches_domain(host, d) for d in self.allowed_domains)

    def is_payment_host(self, host: str) -> bool:
        return any(_matches_domain(host, d) for d in self.cfg.blocked_payment_domains)

    def check_navigation(self, url: str) -> str | None:
        """For URLs the agent asks to open directly, or links it clicks."""
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return self._block("navigate", url, f"'{scheme}:' links are not allowed")
        host = _host(url)
        if self.is_payment_host(host):
            return self._block("navigate", url, f"payment site blocked ({host})")
        if not self.is_allowed_host(host):
            return self._block("navigate", url, f"outside allowed domains ({host})")
        return None

    def check_request(self, url: str, method: str, top_level_navigation: bool) -> str | None:
        """Called by the browser for EVERY network request."""
        if url.startswith(("data:", "blob:", "about:")):
            return None
        host = _host(url)
        if self.is_payment_host(host):
            return self._block("request", url, f"payment domain blocked ({host})")
        if top_level_navigation and not self.is_allowed_host(host):
            return self._block("request", url, f"page outside allowed domains ({host})")
        if self.cfg.safe_mode == "strict" and method.upper() not in SAFE_METHODS:
            return self._block("request", f"{method} {url}",
                               "strict safe mode: data-changing request blocked")
        return None

    # ----------------------------------------------------------------- clicks
    def check_click(self, el: dict[str, Any]) -> str | None:
        label = " ".join(filter(None, [el.get("name"), el.get("attrs"), el.get("value")]))
        desc = f'[{el.get("id")}] {el.get("tag")} "{el.get("name", "")}"'

        # Hard rules first (can never be overridden), so an "allow" can't skip them.
        href = el.get("href") or ""
        if href and not href.startswith(("#", "javascript:")):
            host = _host(href)
            if host and self.is_payment_host(host):
                return self._block("click", desc, f"link goes to payment site ({host})")
            if host and not self.is_allowed_host(host):
                return self._block("click", desc, f"link leaves allowed domains ({host})")
        if href.startswith(("mailto:", "tel:")):
            return self._block("click", desc, "mailto:/tel: links are not allowed")

        if el.get("tag") == "input" and el.get("type") == "file" and not self.cfg.allow_file_uploads:
            return self._block("click", desc, "file uploads are disabled")

        if el.get("is_submit") and self._has_sensitive_keyword(el.get("form_fields", "")):
            return self._block("click", desc, "form contains card/bank fields; submitting is blocked")

        # Soft rule: a human supervisor may allow these once.
        for word, pattern in self.click_patterns:
            if pattern.search(label):
                return self._block("click", desc, f"button/link contains blocked word '{word}'",
                                   overridable=True)
        return None

    # ----------------------------------------------------------------- typing
    def _has_sensitive_keyword(self, text: str) -> str | None:
        text = (text or "").lower()
        for kw in self.cfg.blocked_field_keywords:
            if kw in text:
                return kw
        return None

    def check_type(self, el: dict[str, Any], text: str) -> str | None:
        desc = f'[{el.get("id")}] {el.get("tag")} "{el.get("name", "")}"'

        kw = self._has_sensitive_keyword(el.get("attrs", ""))
        if kw:
            return self._block("type", desc, f"sensitive field (matches '{kw}')")

        if el.get("type") == "file":
            return self._block("type", desc, "file inputs are not allowed")

        if looks_like_card_number(text):
            return self._block("type", desc, "text looks like a card number")

        unknown = [p for p in PLACEHOLDER_RE.findall(text) if p not in self.secrets]
        if unknown:
            return self._block("type", desc,
                               f"unknown placeholder(s) {unknown}; available: {list(self.secrets) or 'none'}")

        if el.get("type") == "password" and text and not PLACEHOLDER_RE.fullmatch(text.strip()):
            return self._block("type", desc,
                               "password fields only accept a placeholder like {{PASSWORD}} from .env")
        return None

    # ---------------------------------------------------------------- secrets
    def resolve_secrets(self, text: str) -> str:
        """'{{PASSWORD}}' -> real value. Only called at the moment of typing."""
        return PLACEHOLDER_RE.sub(lambda m: self.secrets.get(m.group(1), m.group(0)), text)

    def mask(self, text: str) -> str:
        """Real secret values -> '{{NAME}}' so they never reach logs or reports."""
        if not text:
            return text
        for name, value in self.secrets.items():
            if value:
                text = text.replace(value, "{{" + name + "}}")
        return text
