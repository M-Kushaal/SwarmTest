"""Human-in-the-loop pauses, shared by every agent in a run.

When an agent needs a human (guardrail block, CAPTCHA, credits ran out) it calls
`await supervisor.ask(...)`. The agent waits; all OTHER agents keep running.

- The terminal shows ONE question at a time; other paused agents wait in a queue.
- The dashboard can answer ANY paused agent at any time (even ones still queued).
- No answer before the timeout -> ask() returns None (the caller ends that session).
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel


@dataclass
class PauseRequest:
    id: int
    agent: str
    kind: str                  # "block" | "captcha" | "api"
    title: str
    body: str
    options: list[str]         # human-readable lines, e.g. "Enter -> end this agent's session"
    can_allow: bool            # show an "allow" button on the dashboard
    deadline: float            # time.time() when it auto-ends
    future: asyncio.Future = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "agent": self.agent, "kind": self.kind, "title": self.title,
                "body": self.body, "options": self.options, "can_allow": self.can_allow,
                "deadline": self.deadline}


class Supervisor:
    def __init__(self, console: Console, timeout_s: float):
        self.console = console
        self.timeout_s = timeout_s
        self.pending: dict[int, PauseRequest] = {}
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self._ids = itertools.count(1)
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: PauseRequest | None = None
        self._reader: threading.Thread | None = None
        self._line_waiter: asyncio.Future | None = None
        self._stdin_closed = False

    # ----------------------------------------------------------- terminal input
    def _ensure_started(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._lock = asyncio.Lock()
        if self._reader is None and sys.stdin is not None:
            self._reader = threading.Thread(target=self._read_stdin, daemon=True, name="stdin-reader")
            self._reader.start()

    def _read_stdin(self) -> None:
        """Runs in a background thread so a waiting prompt never freezes the other agents."""
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if line == "":  # EOF (no interactive terminal): answers can still come from the dashboard
                if self._loop:
                    self._loop.call_soon_threadsafe(self._on_stdin_closed)
                return
            if self._loop:
                self._loop.call_soon_threadsafe(self._on_terminal_line, line.rstrip("\r\n"))

    def _on_terminal_line(self, text: str) -> None:
        req = self._active
        if req and not req.future.done():
            req.future.set_result(text)
        elif self._line_waiter and not self._line_waiter.done():
            self._line_waiter.set_result(text)
        # otherwise: lines typed while nothing is being asked are ignored

    def _on_stdin_closed(self) -> None:
        self._stdin_closed = True
        if self._line_waiter and not self._line_waiter.done():
            self._line_waiter.set_result("")

    async def wait_for_enter(self) -> None:
        """Wait for the person to press Enter (used to keep the dashboard open at the end)."""
        self._ensure_started()
        if self._stdin_closed:
            return
        assert self._loop
        self._line_waiter = self._loop.create_future()
        await self._line_waiter

    # ------------------------------------------------------- dashboard input
    def answer_threadsafe(self, pause_id: int, text: str) -> bool:
        """Called from the dashboard's web-server thread."""
        if not self._loop or pause_id not in self.pending:
            return False
        self._loop.call_soon_threadsafe(self._answer, pause_id, text)
        return True

    def _answer(self, pause_id: int, text: str) -> None:
        req = self.pending.get(pause_id)
        if req and not req.future.done():
            req.future.set_result(text)

    # ---------------------------------------------------------------- notify
    def _emit(self, event: dict[str, Any]) -> None:
        for listener in self.listeners:
            try:
                listener(event)
            except Exception:
                pass

    # ------------------------------------------------------------------- ask
    async def ask(self, *, agent: str, kind: str, title: str, body: str, options: list[str],
                  can_allow: bool = False) -> str | None:
        """Pause this agent until a human answers. Returns the answer text, or None on timeout."""
        self._ensure_started()
        assert self._lock and self._loop
        req = PauseRequest(next(self._ids), agent, kind, title, body, options, can_allow,
                           time.time() + self.timeout_s, self._loop.create_future())
        self.pending[req.id] = req
        self._emit({"event": "pause", "agent": agent, **req.public()})
        if self._lock.locked():
            self.console.print(f"[yellow]{escape(f'[{agent}]')} paused ({kind}); waiting in line for the terminal "
                               "- or answer it on the dashboard[/]")

        acquire = asyncio.ensure_future(self._lock.acquire())
        answer: str | None = None
        try:
            done, _ = await asyncio.wait({acquire, req.future}, timeout=self._remaining(req),
                                         return_when=asyncio.FIRST_COMPLETED)
            if req.future.done():
                answer = req.future.result()
            elif acquire in done:
                self._active = req
                self._print_prompt(req)
                try:
                    answer = await asyncio.wait_for(asyncio.shield(req.future), timeout=self._remaining(req))
                except asyncio.TimeoutError:
                    answer = None
        finally:
            if self._active is req:
                self._active = None
            if not acquire.cancel() and not acquire.cancelled() and acquire.exception() is None:
                self._lock.release()  # we held the terminal; hand it to the next paused agent
            self.pending.pop(req.id, None)
            outcome = "timed out" if answer is None else f'answered: "{answer}"' if answer else "answered: (Enter)"
            self.console.print(f"[dim]{escape(f'[{agent}]')} pause {escape(outcome)}[/]")
            self._emit({"event": "pause_resolved", "agent": agent, "id": req.id,
                        "answer": answer, "timed_out": answer is None})
        return answer

    @staticmethod
    def _remaining(req: PauseRequest) -> float:
        return max(0.0, req.deadline - time.time())

    def _print_prompt(self, req: PauseRequest) -> None:
        mins, secs = divmod(int(self._remaining(req)), 60)
        options = "\n".join(f"  {o}" for o in req.options)
        self.console.print(Panel(
            f"{escape(req.body)}\n\n{escape(options)}\n\n[dim]Type your answer below and press Enter "
            f"(or answer on the dashboard). Auto-ends in {mins}m {secs:02d}s.[/]",
            title=escape(f"[{req.agent}] PAUSED: {req.title}"), border_style="red"))
