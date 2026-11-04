"""Live dashboard: a local web page showing the whole run while it's going.

    http://127.0.0.1:8765   (only reachable from this computer)

Overview first (run outcome -> what needs you -> cross-agent findings -> compact agent
cards), with a per-agent inspector (overview / replay / events / report) one click away.
Uses only the Python standard library (a tiny HTTP server in a background thread).
Answers to pauses require a per-run secret token, so other websites open in your browser
can't press the buttons for you.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from testswarm.supervisor import Supervisor

SHOT_RE = re.compile(r"^step_\d{1,4}\.jpg$")
BLOCK_REASON_RE = re.compile(r"BLOCKED by safety rule: (.*?)(?:\. The human supervisor|\. Session ended|$)")
FEED_SIZE = 500
STEP_KEYS = ("step", "url", "title", "screenshot", "action_desc", "result", "blocked", "failed", "thought",
             "observation", "confusion", "emotion", "progress", "target_box")


class DashboardState:
    """Thread-safe store fed by logger + supervisor events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.agents: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, list[dict[str, Any]]] = {}     # full step history per agent (inspector only)
        self.pauses: dict[int, dict[str, Any]] = {}
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_SIZE)
        self.started = time.time()
        self.finished = False
        self.meta: dict[str, Any] = {"run_name": "", "summary": False}

    def set_meta(self, run_name: str) -> None:
        with self._lock:
            self.meta["run_name"] = run_name

    def set_summary(self, path: Path) -> None:
        with self._lock:
            self.meta["summary"] = True
            self.meta["summary_path"] = str(path)

    def _blank(self, name: str) -> dict[str, Any]:
        return {"name": name, "status": "queued", "phase": "waiting for a free browser", "step": 0,
                "max_steps": None, "persona": "", "objective": "", "url": "", "title": "", "screenshot": None,
                "thought": "", "action": "", "result": "", "blocked": False, "target_box": None,
                "viewport": [1280, 800], "confusion": None, "emotion": "", "confusion_history": [],
                "blocked_count": 0, "blocks": [], "notes": [], "summary": "", "tokens": 0, "seconds": 0,
                "report": None, "analysis": None, "run_dir": None, "started_at": None, "ended_at": None}

    def register(self, name: str, persona: str, objective: str, url: str, max_steps: int) -> None:
        with self._lock:
            self.steps[name] = []
            self.agents[name] = {**self._blank(name), "persona": persona, "objective": objective,
                                 "url": url, "max_steps": max_steps}

    def _feed(self, agent: str, kind: str, text: str, step: int | None = None, important: bool = False) -> None:
        self.feed.append({"t": time.strftime("%H:%M:%S"), "ts": time.time(), "agent": agent, "kind": kind,
                          "text": text[:400], "step": step, "important": important})

    def on_event(self, e: dict[str, Any]) -> None:
        name = e.get("agent", "?")
        kind = e.get("event")
        with self._lock:
            if name not in self.agents:
                self.steps[name] = []
                self.agents[name] = {**self._blank(name), "status": "running"}
            a = self.agents[name]
            if kind == "run_started":
                a.update(status="running", phase="starting", run_dir=e.get("run_dir"), started_at=time.time(),
                         max_steps=e.get("max_steps", a.get("max_steps")), url=e.get("url", a.get("url")),
                         viewport=e.get("viewport") or a.get("viewport"))
                self._feed(name, "start", "Session started")
            elif kind == "phase":
                a["phase"] = e.get("phase", "")
                a["step"] = max(a.get("step") or 0, e.get("step") or 0)
                for k in ("screenshot", "url", "title"):
                    if e.get(k):
                        a[k] = e[k]
            elif kind == "step":
                self.steps[name].append({k: e.get(k) for k in STEP_KEYS})
                a.update(step=e["step"], thought=e.get("thought", ""), action=e.get("action_desc", ""),
                         result=e.get("result", ""), blocked=e.get("blocked", False),
                         confusion=e.get("confusion"), emotion=e.get("emotion", ""),
                         url=e.get("url", a.get("url")), title=e.get("title", ""),
                         target_box=e.get("target_box"), screenshot=e.get("screenshot", a.get("screenshot")))
                a["confusion_history"].append(e.get("confusion"))
                action = e.get("action") or {}
                if action.get("type") == "note" and action.get("text"):
                    a["notes"].append({"step": e["step"], "note": action["text"]})
                if e.get("blocked"):
                    a["blocked_count"] += 1
                    m = BLOCK_REASON_RE.search(e.get("result") or "")
                    a["blocks"].append({"step": e["step"], "action": e.get("action_desc", ""),
                                        "reason": m.group(1) if m else "guardrail"})
                    self._feed(name, "blocked", f"Blocked by a guardrail: {e.get('action_desc', '')}",
                               e["step"], important=True)
                elif e.get("failed"):
                    self._feed(name, "failed", f"Action failed: {e.get('action_desc', '')}", e["step"],
                               important=True)
                elif action.get("type") == "note":
                    self._feed(name, "note", f"Noted: {action.get('text', '')}", e["step"], important=True)
                else:
                    self._feed(name, "step", f"{e.get('action_desc', '')} -> {e.get('result', '')}", e["step"])
            elif kind in ("warning", "info"):
                self._feed(name, kind, e.get("message", ""), a.get("step"))
            elif kind == "pause":
                self.pauses[e["id"]] = {k: e[k] for k in
                                        ("id", "agent", "kind", "title", "body", "options", "can_allow", "deadline")}
                a["status"] = "paused"
                self._feed(name, "input", f"Needs your input: {e.get('title')}", a.get("step"), important=True)
            elif kind == "pause_resolved":
                self.pauses.pop(e.get("id"), None)
                if a.get("status") == "paused":
                    a["status"] = "running"
                txt = ("Input request timed out" if e.get("timed_out")
                       else f"You answered: {e.get('answer') or '(Enter)'}")
                self._feed(name, "timeout" if e.get("timed_out") else "answered", txt, a.get("step"), important=True)
            elif kind == "run_finished":
                a.update(status=e.get("status"), phase="finished", summary=e.get("summary", ""),
                         tokens=e.get("tokens", 0), seconds=e.get("seconds", 0), ended_at=time.time())
                self._feed(name, "finish", f"Session ended: {e.get('status')}", a.get("step"), important=True)
            elif kind == "report_written":
                a["report"] = e.get("path")
                a["analysis"] = _load_analysis(Path(e.get("path", "")).with_suffix(".json"))
                self._feed(name, "report", "Report written", a.get("step"))

    def snapshot(self) -> dict[str, Any]:
        """Lightweight state for the overview (polled every second). Step history is not included."""
        with self._lock:
            agents = []
            for a in self.agents.values():
                b = dict(a)
                an = a.get("analysis") or {}
                b["analysis"] = ({k: an.get(k) for k in ("objective_achieved", "usability_score", "summary",
                                                         "friction_points")} if an else None)
                agents.append(b)
            return json.loads(json.dumps({
                "agents": agents, "pauses": list(self.pauses.values()), "feed": list(self.feed)[-250:],
                "started": self.started, "now": time.time(), "finished": self.finished, "meta": self.meta,
            }, default=str))

    def agent_detail(self, name: str) -> dict[str, Any] | None:
        """Everything about one agent, for the inspector."""
        with self._lock:
            if name not in self.agents:
                return None
            return json.loads(json.dumps({
                "agent": self.agents[name], "steps": self.steps.get(name, []),
                "events": [f for f in self.feed if f["agent"] == name][-200:],
                "pauses": [p for p in self.pauses.values() if p["agent"] == name], "now": time.time(),
            }, default=str))

    def run_dir_of(self, name: str) -> Path | None:
        with self._lock:
            d = self.agents.get(name, {}).get("run_dir")
        return Path(d) if d else None

    def report_of(self, name: str) -> Path | None:
        with self._lock:
            r = self.agents.get(name, {}).get("report")
        return Path(r) if r else None

    def summary_path(self) -> Path | None:
        with self._lock:
            p = self.meta.get("summary_path")
        return Path(p) if p else None


def _load_analysis(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    an = dict(data.get("analysis") or {})
    an["report_status"] = data.get("status")
    return an


class Dashboard:
    def __init__(self, supervisor: Supervisor, port: int):
        self.state = DashboardState()
        self.supervisor = supervisor
        self.token = secrets.token_urlsafe(16)
        self.port = port
        self.url = ""
        self._server: ThreadingHTTPServer | None = None
        supervisor.listeners.append(self.state.on_event)

    def start(self, open_browser: bool) -> str:
        handler = _make_handler(self)
        last_error: Exception | None = None
        for port in range(self.port, self.port + 20):  # if the port is busy, try the next ones
            try:
                self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                break
            except OSError as e:
                last_error = e
        if self._server is None:
            raise RuntimeError(f"Could not start dashboard: {last_error}")
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True, name="dashboard").start()
        if open_browser:
            webbrowser.open(self.url)
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the terminal clean
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # screenshots are cached so the 1-second refresh doesn't re-download them
            self.send_header("Cache-Control", "private, max-age=300" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                html = PAGE.replace("__TOKEN__", dash.token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._send(200, json.dumps(dash.state.snapshot()).encode("utf-8"), "application/json")
            elif u.path == "/shot":
                name, file = q.get("agent", [""])[0], q.get("file", [""])[0]
                run_dir = dash.state.run_dir_of(name)
                path = run_dir / "screenshots" / file if run_dir and SHOT_RE.match(file) else None
                if path and path.is_file():
                    self._send(200, path.read_bytes(), "image/jpeg", cache=True)
                else:
                    self._send(404, b"not found", "text/plain")
            elif u.path == "/api/agent":
                d = dash.state.agent_detail(q.get("agent", [""])[0])
                if d is None:
                    self._send(404, b"no such agent", "text/plain")
                else:
                    self._send(200, json.dumps(d).encode("utf-8"), "application/json")
            elif u.path == "/summary":
                path = dash.state.summary_path()
                if path and path.is_file():
                    self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
                else:
                    self._send(404, b"summary not written yet", "text/plain")
            elif u.path == "/report":
                path = dash.state.report_of(q.get("agent", [""])[0])
                if path and path.is_file():
                    self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
                else:
                    self._send(404, b"report not written yet", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/answer" or self.headers.get("X-TS-Token") != dash.token:
                self._send(403, b"forbidden", "text/plain")
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 10_000)
                data = json.loads(self.rfile.read(length) or b"{}")
                ok = dash.supervisor.answer_threadsafe(int(data["id"]), str(data.get("text", ""))[:2000])
            except (ValueError, KeyError, json.JSONDecodeError):
                ok = False
            self._send(200 if ok else 409, json.dumps({"ok": ok}).encode(), "application/json")

    return Handler


PAGE = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
