"""The agent's hands and eyes: a real Chromium browser driven by Playwright.

- Launches a brand-new, empty browser profile (no saved cards/passwords/cookies).
- Every network request goes through Guard.check_request (domain lock, payments, strict mode).
- observe(): screenshot with numbered pink labels + list of clickable elements + page info.
- Actions: click, type, select, scroll, go_back, navigate, wait.
- Collects technical problems (JS errors, failed requests, HTTP 4xx/5xx) for the report.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Error as PlaywrightError,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)

from swarmtest.config import BrowserSettings
from swarmtest.guardrails import Guard

MAX_ELEMENTS = 120
MAX_ISSUES = 200
EVAL_TIMEOUT_S = 10        # max wait for in-page JavaScript (Playwright has no limit of its own)
SCREENSHOT_TIMEOUT_S = 15
OBSERVE_ATTEMPTS = 3       # tries to read a page; the last one reloads it first


class PageStuck(Exception):
    """The page stopped responding even after retries and a reload."""

# Runs inside the web page. Finds visible interactive elements, tags each with
# data-ts-id="N", and draws numbered labels so the screenshot shows the ids.
COLLECT_JS = r"""
(maxElements) => {
  document.querySelectorAll('[data-ts-id]').forEach(e => e.removeAttribute('data-ts-id'));
  document.getElementById('__ts_overlay')?.remove();

  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=switch], [role=combobox], [contenteditable=""], [contenteditable=true], [onclick]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map(l => l.innerText).join(' '));
    const ids = el.getAttribute('aria-labelledby');
    if (ids) return clean(ids.split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' '));
    return '';
  };
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= maxElements) break;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
    const hit = document.elementFromPoint(cx, cy);
    if (hit && !(hit === el || el.contains(hit) || hit.contains(el))) continue;  // covered (e.g. by a modal)

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    const img = el.querySelector ? el.querySelector('img[alt]') : null;
    const name = clean(
      el.getAttribute('aria-label') || labelFor(el) ||
      (isField ? '' : el.innerText) || el.getAttribute('title') || el.getAttribute('alt') ||
      (img ? img.getAttribute('alt') : '') ||
      ((tag === 'input' && (type === 'submit' || type === 'button')) ? el.value : '')
    ).slice(0, 100);
    const form = el.form || el.closest('form');
    const formFields = form ? Array.from(form.querySelectorAll('input,select,textarea')).map(f =>
      [f.name, f.id, f.getAttribute('autocomplete'), f.getAttribute('placeholder'),
       f.getAttribute('aria-label')].filter(Boolean).join(' ')).join(' ').toLowerCase().slice(0, 2000) : '';
    const id = out.length + 1;
    el.setAttribute('data-ts-id', String(id));
    out.push({
      id, tag, type,
      role: el.getAttribute('role') || '',
      name,
      href: tag === 'a' ? (el.getAttribute('href') ? el.href : '') : '',
      value: (isField && tag !== 'select' && type !== 'password') ? clean(el.value).slice(0, 60)
             : (tag === 'select' && el.selectedOptions[0] ? clean(el.selectedOptions[0].text) : ''),
      placeholder: clean(el.getAttribute('placeholder')).slice(0, 60),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: !!el.checked,
      attrs: [el.getAttribute('name'), el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
              el.getAttribute('aria-label'), labelFor(el), el.getAttribute('data-testid')]
              .filter(Boolean).join(' ').toLowerCase().slice(0, 300),
      is_submit: (tag === 'button' && (type === 'submit' || (type === '' && !!form))) ||
                 (tag === 'input' && (type === 'submit' || type === 'image')),
      form_fields: formFields,
      options: tag === 'select' ? Array.from(el.options).slice(0, 25).map(o => clean(o.text)) : [],
      box: {x: r.left, y: r.top, w: r.width, h: r.height},
    });
  }

  const ov = document.createElement('div');
  ov.id = '__ts_overlay';
  ov.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  for (const e of out) {
    const b = document.createElement('div');
    b.style.cssText = `position:fixed;left:${e.box.x}px;top:${e.box.y}px;width:${e.box.w}px;` +
      `height:${e.box.h}px;outline:2px solid rgba(255,0,110,.85);`;
    const t = document.createElement('div');
    t.textContent = e.id;
    t.style.cssText = `position:fixed;left:${Math.max(e.box.x - 2, 0)}px;top:${Math.max(e.box.y - 14, 0)}px;` +
      'background:#ff006e;color:#fff;font:bold 11px/13px monospace;padding:0 3px;border-radius:3px;';
    ov.append(b, t);
  }
  document.documentElement.appendChild(ov);
  return out;
}
"""

PAGE_INFO_JS = r"""
() => {
  const body = document.body ? document.body.innerText : '';
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], ' +
    '.g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form, #cf-challenge-running');
  return {
    title: document.title || '',
    text: body.replace(/\n\s*\n+/g, '\n').slice(0, 4000),
    scrollY: Math.round(window.scrollY),
    scrollHeight: document.documentElement.scrollHeight,
    viewportHeight: window.innerHeight,
    captcha: captcha || /verify you are human|are you a robot|checking your browser/i.test(body.slice(0, 3000)),
  };
}
"""

REMOVE_OVERLAY_JS = "() => document.getElementById('__ts_overlay')?.remove()"

HIGHLIGHT_JS = r"""
(el) => {
  const prev = [el.style.outline, el.style.outlineOffset];
  el.style.outline = '4px solid #ff1e1e';
  el.style.outlineOffset = '2px';
  return prev;
}
"""
UNHIGHLIGHT_JS = "(el, prev) => { el.style.outline = prev[0]; el.style.outlineOffset = prev[1]; }"


@dataclass
class Observation:
    step: int
    url: str
    title: str
    elements: list[dict[str, Any]]
    text: str
    scroll_y: int
    scroll_height: int
    viewport_height: int
    captcha: bool
    screenshot_b64: str
    screenshot_path: Path

    def element(self, element_id: int | None) -> dict[str, Any] | None:
        return next((e for e in self.elements if e["id"] == element_id), None)


@dataclass
class TechIssues:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    http_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)

    @staticmethod
    def _add(bucket: list[str], item: str) -> None:
        if len(bucket) < MAX_ISSUES and item not in bucket:
            bucket.append(item)


class BrowserSession:
    def __init__(self, cfg: BrowserSettings, guard: Guard, run_dir: Path, allow_downloads: bool,
                 window_index: int = 0):
        self.window_index = window_index  # multi-agent: cascade windows so they don't fully overlap
        self._attached: set[int] = set()
        self._start_url = ""
        self.cfg = cfg
        self.guard = guard
        self.run_dir = run_dir
        self.shots_dir = run_dir / "screenshots"
        self.allow_downloads = allow_downloads
        self.issues = TechIssues()
        self._blocked_urls: set[str] = set()
        self._new_pages: list[Page] = []
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self, url: str) -> str | None:
        """Open a clean browser and go to the start URL. Returns an error string or None."""
        self._start_url = url
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        i = self.window_index
        self._browser = await self._pw.chromium.launch(
            headless=self.cfg.headless, slow_mo=self.cfg.slow_mo_ms,
            args=[f"--window-position={(i % 5) * 70 + (i // 5) * 400},{(i % 5) * 55}"])
        # new_context() = fresh, isolated profile: no saved cards, passwords, cookies or autofill.
        self._context = await self._browser.new_context(
            viewport={"width": self.cfg.viewport.width, "height": self.cfg.viewport.height},
            accept_downloads=self.allow_downloads,
            service_workers="block",  # so every request is visible to the guard
        )
        self._context.set_default_timeout(self.cfg.page_load_timeout_s * 1000)
        await self._context.route("**/*", self._route)
        self._context.on("page", self._on_new_page)
        if self.cfg.save_trace:
            await self._context.tracing.start(screenshots=True, snapshots=True)
        self.page = await self._context.new_page()
        self._attach(self.page)
        await asyncio.sleep(0)  # let the 'page' event for this first tab fire...
        self._new_pages = [p for p in self._new_pages if p is not self.page]  # ...and forget it: not a "new tab"
        return await self._goto(url)

    async def close(self) -> None:
        """Always finishes: every step has a time limit so a stuck browser can't freeze the run."""
        async def safe(coro, seconds: float) -> None:
            try:
                await asyncio.wait_for(coro, seconds)
            except (PlaywrightError, asyncio.TimeoutError):
                pass

        if self._context and self.cfg.save_trace:
            await safe(self._context.tracing.stop(path=str(self.run_dir / "trace.zip")), 30)
        if self._context:
            await safe(self._context.close(), 15)
        if self._browser:
            await safe(self._browser.close(), 15)
        if self._pw:
            await safe(self._pw.stop(), 15)

    # ------------------------------------------------------- network guard
    async def _route(self, route: Route) -> None:
        req = route.request
        top_level = False
        try:
            top_level = req.is_navigation_request() and req.frame.parent_frame is None
        except PlaywrightError:
            pass
        reason = self.guard.check_request(req.url, req.method, top_level)
        if reason:
            self._blocked_urls.add(req.url)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    # ------------------------------------------------------ event listeners
    def _attach(self, page: Page) -> None:
        if id(page) in self._attached:
            return
        self._attached.add(id(page))
        page.on("console", lambda m: m.type == "error" and TechIssues._add(
            self.issues.console_errors, self.guard.mask(m.text)[:300]))
        page.on("pageerror", lambda e: TechIssues._add(
            self.issues.page_errors, self.guard.mask(str(e))[:300]))
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("dialog", self._on_dialog)

    def _on_response(self, resp: Response) -> None:
        host = (urlparse(resp.url).hostname or "").lower()
        if resp.status >= 400 and self.guard.is_allowed_host(host):
            TechIssues._add(self.issues.http_errors,
                            f"HTTP {resp.status} {resp.request.method} {self.guard.mask(resp.url)[:200]}")

    def _on_request_failed(self, req: Request) -> None:
        if req.url in self._blocked_urls:
            return  # we blocked it on purpose; not an app bug
        host = (urlparse(req.url).hostname or "").lower()
        if self.guard.is_allowed_host(host):
            TechIssues._add(self.issues.failed_requests,
                            f"{req.method} {self.guard.mask(req.url)[:200]} ({req.failure or 'failed'})")

    async def _on_dialog(self, dialog: Dialog) -> None:
        TechIssues._add(self.issues.dialogs, f"{dialog.type}: {dialog.message[:200]}")
        try:
            await dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_new_page(self, page: Page) -> None:
        self._attach(page)
        self._new_pages.append(page)

    async def _adopt_new_tabs(self) -> str:
        """If the last action opened a new tab, switch to it (if it's on an allowed domain)."""
        note = ""
        while self._new_pages:
            p = self._new_pages.pop(0)
            if p is self.page:
                continue
            try:
                await p.wait_for_load_state("domcontentloaded", timeout=8000)
            except PlaywrightError:
                pass
            host = (urlparse(p.url).hostname or "").lower()
            if p.url.startswith("http") and self.guard.is_allowed_host(host):
                self.page = p
                await p.bring_to_front()
                note = f" (opened a new tab: {p.url})"
            else:
                self.guard._block("popup", p.url, "new tab outside allowed domains was closed")
                await p.close()
                note = " (a new tab to another site was blocked and closed)"
        return note

    # ------------------------------------------------------------- helpers
    async def _settle(self) -> None:
        assert self.page
        for state, ms in (("domcontentloaded", self.cfg.page_load_timeout_s * 1000), ("networkidle", 2500)):
            try:
                await self.page.wait_for_load_state(state, timeout=ms)
            except PlaywrightError:
                pass

    async def _goto(self, url: str) -> str | None:
        assert self.page
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            return _short_error(e)
        await self._settle()
        return None

    async def _eval(self, js: str, arg: Any = None, timeout: float = EVAL_TIMEOUT_S) -> Any:
        """page.evaluate has NO built-in time limit; if the page is mid-navigation it can wait forever."""
        assert self.page
        try:
            return await asyncio.wait_for(self.page.evaluate(js, arg), timeout)
        except asyncio.TimeoutError:
            raise PageStuck(f"page did not respond within {timeout:g}s") from None

    # ---------------------------------------------------------------- eyes
    async def observe(self, step: int) -> Observation:
        """Read the page. Retries, then reloads; raises PageStuck only if the page truly won't respond."""
        assert self.page
        last: Exception | None = None
        for attempt in range(OBSERVE_ATTEMPTS):
            if last is not None and "crash" in str(last).lower():  # the tab crashed: open a fresh one
                await self._replace_crashed_tab()
            elif attempt == OBSERVE_ATTEMPTS - 1:  # last resort: reload the page
                try:
                    await self.page.reload(wait_until="domcontentloaded",
                                           timeout=self.cfg.page_load_timeout_s * 1000)
                except PlaywrightError:
                    pass
            try:
                return await self._observe_once(step)
            except (PlaywrightError, PageStuck) as e:  # navigated mid-read, or hung: wait and retry
                last = e
                await asyncio.sleep(1 + attempt)
        raise PageStuck(f"could not read the page after {OBSERVE_ATTEMPTS} tries ({_short_error(last)})")

    async def _replace_crashed_tab(self) -> None:
        """Chromium tabs can crash under heavy load; continue in a new tab on the same page."""
        assert self._context and self.page
        url = self.page.url if self.page.url.startswith("http") else self._start_url
        try:
            await asyncio.wait_for(self.page.close(), 5)
        except (PlaywrightError, asyncio.TimeoutError):
            pass
        self.page = await self._context.new_page()
        self._attach(self.page)
        await asyncio.sleep(0)
        self._new_pages = [p for p in self._new_pages if p is not self.page]
        await self._goto(url)

    async def _observe_once(self, step: int) -> Observation:
        assert self.page
        await self._settle()
        elements = await self._eval(COLLECT_JS, MAX_ELEMENTS)
        info = await self._eval(PAGE_INFO_JS)
        path = self.shots_dir / f"step_{step:02d}.jpg"
        png = await self.page.screenshot(path=str(path), type="jpeg", quality=75,
                                         timeout=SCREENSHOT_TIMEOUT_S * 1000)
        try:
            await self._eval(REMOVE_OVERLAY_JS, timeout=3)
        except (PlaywrightError, PageStuck):
            pass
        return Observation(
            step=step, url=self.page.url, title=info["title"], elements=elements,
            text=self.guard.mask(info["text"]), scroll_y=info["scrollY"],
            scroll_height=info["scrollHeight"], viewport_height=info["viewportHeight"],
            captcha=info["captcha"], screenshot_b64=base64.standard_b64encode(png).decode(),
            screenshot_path=path,
        )

    # --------------------------------------------------------------- hands
    def _locator(self, element_id: int):
        assert self.page
        return self.page.locator(f'[data-ts-id="{element_id}"]').first

    async def _highlight(self, loc) -> None:
        if self.cfg.highlight_before_click_ms <= 0:
            return
        try:
            prev = await loc.evaluate(HIGHLIGHT_JS)
            await asyncio.sleep(self.cfg.highlight_before_click_ms / 1000)
            await loc.evaluate(UNHIGHLIGHT_JS, prev)
        except PlaywrightError:
            pass

    async def click(self, el: dict[str, Any], highlight: bool = True) -> str:
        loc = self._locator(el["id"])
        if highlight:
            await self._highlight(loc)
        try:
            await loc.click(timeout=6000)
        except PlaywrightError as e:
            return f"click failed: {_short_error(e)}"
        await self._settle()
        return "clicked" + await self._adopt_new_tabs()

    async def type_text(self, el: dict[str, Any], real_text: str, press_enter: bool) -> str:
        loc = self._locator(el["id"])
        await self._highlight(loc)
        try:
            if el["tag"] == "select":
                await loc.select_option(label=real_text, timeout=6000)
                result = "selected option"
            else:
                await loc.fill(real_text, timeout=6000)
                result = "typed"
            if press_enter:
                await loc.press("Enter")
                result += " and pressed Enter"
        except PlaywrightError as e:
            return f"typing failed: {_short_error(e)}"
        await self._settle()
        return result + await self._adopt_new_tabs()

    async def scroll(self, direction: str) -> str:
        assert self.page
        dy = int(self.cfg.viewport.height * 0.8) * (-1 if direction == "up" else 1)
        await self.page.mouse.wheel(0, dy)
        await asyncio.sleep(0.6)
        return f"scrolled {direction}"

    async def go_back(self) -> str:
        assert self.page
        try:
            resp = await self.page.go_back(wait_until="domcontentloaded")
        except PlaywrightError as e:
            return f"go back failed: {_short_error(e)}"
        await self._settle()
        return "went back" if resp is not None else "there is no previous page"

    async def navigate(self, url: str) -> str:
        assert self.page
        err = await self._goto(url)
        return f"navigation failed: {err}" if err else f"opened {self.page.url}"

    def absolute_url(self, url: str) -> str:
        assert self.page
        return urljoin(self.page.url, url)

    async def wait(self, seconds: float) -> str:
        await asyncio.sleep(max(0.5, min(seconds, 5)))
        await self._settle()
        return f"waited {seconds:g}s"


def _short_error(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return msg[:200]
