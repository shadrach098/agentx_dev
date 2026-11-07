"""The agentx app window (``agentx --ui``): the same agentx, drawn in a browser window instead of the terminal.

The chat app (``ChatApp``) runs unchanged on a worker thread, exactly as in full-screen mode; this module is only a
different front end. What the app shows goes to the window as events: the agent's tool calls, results and answers as
structured items (``WebScreen``), everything else it prints as styled terminal text (its rich ``Console`` writes ANSI
into ``_Sink``). What the app asks (``read``, ``read_short``, ``select`` and the approval prompts) becomes a card in the
window and waits for the click. The status (working or not, the team's steps, messages waiting) is sent whenever it
changes.

The server listens on 127.0.0.1 only. Every request to the API must carry the random token of this run (the window's
address has it), and the Host header must name this server, so a web page in another tab cannot drive the agent.
The page is opened as an app window (Edge or Chrome ``--app``) when one is found, else in the default browser."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import Future
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from rich.console import Console

from agentx_dev.cli.approval import DECLINED, Answer
from agentx_dev.cli.preview import preview_for
from agentx_dev.cli.ui import CANCELLED, Screen, plain
from agentx_dev.cli.ui_text import HELPER_LOOKS, helper_activity, step_counts, step_stats, summarize_result


class _QuietServer(ThreadingHTTPServer):
    """A window that reloads or closes drops its connections; that is normal, not a traceback for the terminal."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


RESOURCES = Path(__file__).resolve().parent.parent / "resources" / "webui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
CONSOLE_WIDTH = 100
MAX_EVENTS = 5000                   # kept for a window that reconnects (a reload)
MAX_BODY = 15 * 1024 * 1024         # a pasted screenshot, base64
MAX_UPLOAD = 10 * 1024 * 1024
UPLOAD_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
TICK_SEC = 0.25
RECORDING_MARK = "Recording"        # the prompt /voice shows while it records (app_voice.RECORDING)
MODEL_SUGGESTIONS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "gpt-6.1-sol",
                     "gpt-6-luna", "gpt-5.5")
_MAX_RESULT_LINES = 12


class Bus:
    """The events for the window, numbered; a window asks for the ones after the last it has (``after``)."""

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._next = 1
        self._changed = threading.Condition()

    def emit(self, event_type: str, /, **data: Any) -> Dict[str, Any]:
        with self._changed:
            event = {**data, "seq": self._next, "type": event_type}
            self._next += 1
            self._events.append(event)
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]
            self._changed.notify_all()
            return event

    def after(self, seq: int, wait: float = 0.0) -> List[Dict[str, Any]]:
        """The events after ``seq``; waits up to ``wait`` seconds for one when there is none."""
        with self._changed:
            if wait and (not self._events or self._events[-1]["seq"] <= seq):
                self._changed.wait(wait)
            if self._events and seq < self._events[0]["seq"] - 1:
                return [{"seq": self._events[0]["seq"] - 1, "type": "reset"}] + list(self._events)
            return [event for event in self._events if event["seq"] > seq]

    def last(self) -> int:
        with self._changed:
            return self._next - 1

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()


class _Sink(io.TextIOBase):
    """Where the app's console writes: each write is one ``print`` event (ANSI text, drawn in the window)."""

    encoding = "utf-8"

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__()
        self._write = write

    def write(self, text: str) -> int:                       # type: ignore[override]
        self._write(str(text))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def writable(self) -> bool:
        return True


def _text(value: Any, limit: int = 4000) -> str:
    try:
        text = plain("" if value is None else value, keep_newlines=True)
    except Exception:
        return ""
    return text if len(text) <= limit else text[:limit] + " ..."


class WebScreen(Screen):
    """``Screen`` for the window: the agent's own steps go out as items (a tool call and its result, a note, the
    answer, notices); everything the base class prints otherwise (team lines, tables) still goes through the console,
    as terminal text."""

    def __init__(self, console: Console, bus: Bus) -> None:
        super().__init__(console)
        self.bus = bus
        self._notes: List[str] = []

    def show_event(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        name = _text(event.get("name"), 120)
        if kind == "thought":                        # held: narration before a look-around step or the answer is
            text = _text(event.get("content")).strip()   # dropped, as on the terminal; a note before a change is shown
            if text:
                self._notes.append(text)
            return
        if kind == "final" or (kind == "tool_call" and name in HELPER_LOOKS):
            self._notes = []
        elif kind == "tool_call":
            self.flush()
        if kind == "tool_call":
            args = event.get("args")
            try:
                from agentx_dev.cli.approval import subject_of
                subject = self._relative(subject_of(name, args if isinstance(args, dict) else {}))
            except Exception:
                subject = ""
            self.bus.emit("tool_call", name=name, subject=_text(subject, 400), look=name in HELPER_LOOKS)
        elif kind == "tool_result":
            raw = event.get("result")
            try:
                raw_text = str("" if raw is None else raw)
            except Exception:
                raw_text = ""
            text = _text(raw_text, 20_000)
            failed = bool(event.get("is_error")) or text.lstrip().startswith(("ERROR", "Refused", "The user declined"))
            summary, failed_command = summarize_result(name, raw_text) if not failed else ("", False)
            lines = raw_text.replace("\r\n", "\n").split("\n")        # ANSI colours kept: the window draws them
            self.bus.emit("tool_result", name=name, summary=_text(summary, 300), failed=failed or failed_command,
                          detail="\n".join(line[:400] for line in lines[:_MAX_RESULT_LINES]) if failed else "",
                          more=max(0, len(lines) - _MAX_RESULT_LINES) if failed else 0)
        elif kind == "final":
            self.bus.emit("final", text=_text(event.get("content"), 200_000))

    def flush(self) -> None:
        notes, self._notes = self._notes, []
        for text in notes:
            self.bus.emit("thought", text=text)

    def flow_block(self) -> None:
        return None

    def notice(self, text: str) -> None:
        self.bus.emit("notice", level="info", text=_text(text))

    def error(self, text: str) -> None:
        self.bus.emit("notice", level="error", text=_text(text))

    def warning(self, text: str, hint: str = "") -> None:
        self.bus.emit("notice", level="warning", text=_text(text), hint=_text(hint))

    def callout(self, title: str, body: str = "") -> None:
        self.bus.emit("notice", level="callout", text=_text(title), hint=_text(body))

    def welcome(self, **parts: Any) -> None:
        tips = parts.get("tips")
        if isinstance(tips, (list, tuple)):              # the terminal's keys, said the window's way
            parts["tips"] = [str(t).replace("Alt+Enter adds a line", "Shift+Enter adds a line")
                             .replace("Shift+Tab changes the mode", "the menu under the message box changes the mode")
                             for t in tips]
        self.bus.emit("welcome", **{key: (value if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
                                          else _text(value, 400)) for key, value in parts.items()})

    def team_plan(self, steps: Any) -> None:
        shown = []
        for step in steps if isinstance(steps, (list, tuple)) else []:
            if isinstance(step, dict):
                new = step.get("new")
                shown.append({"agent": _text(step.get("agent"), 120), "label": _text(step.get("label"), 80),
                              "query": _text(step.get("query"), 600),
                              "tools": [_text(t, 60) for t in new] if isinstance(new, list) else None})
        self.bus.emit("team_plan", steps=shown)


def _text_mode_step(text: str) -> Optional[tuple]:
    """``(action, action_input)`` when an assistant message is a text-mode step (``{"action": ..., ...}``), else
    None (ordinary text)."""
    stripped = str(text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}") and '"action"' in stripped):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("action"), str):
        return None
    return data["action"], data.get("action_input")


def _app_window_command(url: str) -> Optional[List[str]]:
    """Edge or Chrome started as an app window (no tabs or address bar), when one is installed."""
    candidates: List[str] = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                               os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("msedge", "google-chrome", "chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            return [path, f"--app={url}", "--new-window"]
    return None


def open_window(url: str) -> str:
    """Open ``url`` as an app window, else in the default browser. Returns how it was opened."""
    command = _app_window_command(url)
    if command is not None:
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return "app window"
        except OSError:
            pass
    try:
        if webbrowser.open(url):
            return "browser"
    except Exception:
        pass
    return ""


class WebFront:
    """The window's side of agentx: ``console``, ``read``, ``read_short`` and ``select`` for the app, ``wire(app)``
    once it is built, and ``run(work)``, which serves the window while ``work`` (the app) runs on a worker thread."""

    def __init__(self, *, cwd: Path, home: Path, port: int = 0, open_it: bool = True,
                 opener: Callable[[str], str] = open_window) -> None:
        self.cwd = Path(cwd)
        self.home = Path(home)
        self.port = port
        self.open_it = open_it
        self.opener = opener
        self.token = secrets.token_urlsafe(24)
        self.bus = Bus()
        self.app: Any = None
        self._partial = ""
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._ids = 0
        self._lock = threading.RLock()
        self._closed = False
        self._worker: Optional[threading.Thread] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._last_status = ""
        self._since_wall: Dict[float, float] = {}
        self._conversation = ""
        self.console = Console(file=_Sink(self._printed), force_terminal=True, color_system="truecolor",
                               width=CONSOLE_WIDTH, legacy_windows=False)

    # -- what the app prints and asks ----------------------------------------------------------------------------

    def _printed(self, text: str) -> None:
        with self._lock:
            data = self._partial + text.replace("\r\n", "\n")
            done, sep, self._partial = data.rpartition("\n")
        if sep and done.strip():
            self.bus.emit("print", ansi=done)

    def _ask(self, kind: str, **data: Any) -> Any:
        with self._lock:
            if self._closed:
                raise EOFError()
            self._ids += 1
            ident = self._ids
            future: Future = Future()
            self._pending[ident] = {"kind": kind, "future": future, **data}
        self.bus.emit("ask", id=ident, kind=kind, **{k: v for k, v in data.items() if k != "request"})
        try:
            return future.result()
        finally:
            with self._lock:
                self._pending.pop(ident, None)
            self.bus.emit("answered", id=ident)

    def read(self, prompt: str = "> ", default: str = "") -> str:
        text = self._ask("message", default=str(default or ""))
        self.bus.emit("user", text=_text(text, 100_000))
        return text

    def read_short(self, prompt: str = "") -> str:
        return self._ask("text", prompt=_text(prompt, 2000))

    def select(self, options: Sequence[str], default: int = 0, hotkeys: Optional[Mapping[str, int]] = None) -> Any:
        labels = [_text(option, 600) for option in options]
        if not labels:
            return None
        return self._ask("select", options=labels, default=default if 0 <= default < len(labels) else 0)

    def approve(self, request: Any) -> Answer:
        """The gate's question, as a card: what will happen, the diff or the command, and the answers."""
        app = self.app
        try:
            preview = preview_for(request.tool, request.args, self.cwd,
                                  helper_models=app.helper_models_line() if app is not None and
                                  request.tool == "start_team_job" else "")
            title, body, kind = preview.title, preview.body, preview.kind
            truncated, reason = preview.truncated, preview.reason
        except Exception:
            title, body, kind, truncated, reason = request.tool, "", "text", False, ""
        if truncated:
            self.bus.emit("notice", level="error",
                          text=f"Declined without asking ({_text(request.tool, 60)}): "
                               f"{_text(reason or 'preview unavailable; nothing was run', 300)}.")
            return Answer("n", reason or "preview unavailable")
        helper = ""
        try:
            from agentx_dev.cli.team import current_helper
            helper = str(current_helper() or "")
        except Exception:
            pass
        always = bool(getattr(request, "remember", False) and getattr(request, "grant", ""))
        grant = _text(getattr(request, "grant", ""), 200)
        if always and getattr(request, "kind", "") == "command":
            grant = f"{_text(request.tool, 40)} commands starting with '{grant}'"
        value = self._ask("approval", tool=_text(request.tool, 80), title=_text(title, 2000),
                          subject=_text(getattr(request, "subject", ""), 2000), body=_text(body, 400_000),
                          body_kind=str(kind), helper=_text(helper, 120), always=always, grant=grant)
        if not isinstance(value, dict):
            return Answer("n", "")
        choice = str(value.get("choice") or "n")
        if choice == "a" and not always:
            choice = "y"
        return Answer(choice if choice in ("y", "a", "n") else "n", _text(value.get("reason"), 2000).strip())

    def insert(self, words: str) -> bool:
        self.bus.emit("insert", text=_text(words, 20_000))
        return True

    # -- what the window sends ---------------------------------------------------------------------------------

    def _pending_of(self, kind: str) -> Optional[int]:
        with self._lock:
            for ident, request in self._pending.items():
                if request["kind"] == kind:
                    return ident
        return None

    def answer(self, ident: int, value: Any) -> bool:
        with self._lock:
            request = self._pending.get(int(ident))
        if request is None or request["future"].done():
            return False
        kind = request["kind"]
        if kind == "select":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = CANCELLED
            if not -1 <= value < len(request.get("options") or []):
                value = CANCELLED
        elif kind in ("text", "message"):
            value = "" if value is None else str(value)
        elif kind == "approval" and not isinstance(value, dict):
            value = {"choice": "n"}
        request["future"].set_result(value)
        return True

    def send(self, text: str) -> str:
        """A message from the input box: the answer to the app's ``read`` when it waits for one, else typed while
        the agent works (to the agent at its next step, a team job's helpers, or a command)."""
        text = str(text or "")
        ident = self._pending_of("message")
        if ident is not None:
            self.answer(ident, text)
            return "sent"
        app = self.app
        if app is None:
            return "not ready"
        threading.Thread(target=self._typed, args=(text,), name="agentx-typed", daemon=True).start()
        return "added"

    def _typed(self, text: str) -> None:
        try:
            self.app.typed_while_working(text)
        except Exception as error:
            self.bus.emit("notice", level="error", text=f"Could not take that message ({type(error).__name__}).")

    def stop(self) -> str:
        """The Stop button: like Ctrl+C. An open question is cancelled; else the running turn is interrupted."""
        with self._lock:
            open_requests = [(ident, request) for ident, request in self._pending.items() if request["kind"] != "message"]
        for ident, request in open_requests:
            if request["future"].done():
                continue
            if request["kind"] == "select":
                request["future"].set_result(CANCELLED)
            elif request["kind"] == "approval":
                request["future"].set_result({"choice": "n", "reason": "the user interrupted the question"})
            else:
                request["future"].set_exception(KeyboardInterrupt())
            return "cancelled"
        from agentx_dev.cli.fullscreen import raise_in
        if self._working() and raise_in(self._worker):
            return "interrupted"
        return "idle"

    def voice(self) -> str:
        """The mic button: start or stop ``/voice``, whether the agent is working or not."""
        app = self.app
        if app is None:
            return "not ready"
        if app.recording():
            threading.Thread(target=app.stop_live_voice, args=("",), daemon=True).start()
            return "stopping"
        with self._lock:
            recording = [ident for ident, request in self._pending.items()
                         if request["kind"] == "text" and RECORDING_MARK in str(request.get("prompt", ""))]
        if recording:
            self.answer(recording[0], "")
            return "stopping"
        self.send("/voice")
        return "recording"

    def upload(self, name: str, data: str, media_type: str) -> Dict[str, Any]:
        """A picture pasted or dropped into the window, saved under ``.agentx/uploads`` so the agent can open it."""
        suffix = UPLOAD_TYPES.get(str(media_type or "").lower())
        if suffix is None:
            return {"error": "only PNG, JPEG, GIF and WebP pictures can be added"}
        try:
            raw = base64.b64decode(str(data or ""), validate=True)
        except (binascii.Error, ValueError):
            return {"error": "the picture could not be read"}
        if not raw or len(raw) > MAX_UPLOAD:
            return {"error": "the picture is empty or over 10 MB"}
        folder = self.cwd / ".agentx" / "uploads"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            folder = self.home / ".agentx" / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(str(name or "picture")).stem)[:40].strip("-.") or "picture"
        path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}-{stem}{suffix}"
        path.write_bytes(raw)
        try:
            shown = path.relative_to(self.cwd).as_posix()
        except ValueError:
            shown = str(path)
        return {"path": shown}

    # -- what the window reads ---------------------------------------------------------------------------------

    def _working(self) -> bool:
        progress = getattr(self.app, "progress", None)
        return bool(progress is not None and getattr(progress, "_shown", False))

    def state(self) -> Dict[str, Any]:
        app = self.app
        out: Dict[str, Any] = {"folder": str(self.cwd), "folderName": self.cwd.name or str(self.cwd),
                               "ready": app is not None, "modes": ["default", "accept-edits", "plan", "auto"],
                               "models": list(MODEL_SUGGESTIONS)}
        try:
            from agentx_dev.cli.commands import command_descriptions
            out["commands"] = command_descriptions()
        except Exception:
            out["commands"] = {}
        if app is None:
            return out
        try:
            out["skills"] = dict(app.skill_entries())
        except Exception:
            out["skills"] = {}
        try:
            model = str(app.model_label)
            out["model"] = model
            models = [model, *[str(m) for m in getattr(app, "team_models", []) or []], *MODEL_SUGGESTIONS]
            out["models"] = list(dict.fromkeys(m for m in models if m))
        except Exception:
            pass
        out["mode"] = getattr(app.gate, "mode", "default")
        try:
            from agentx_dev.cli.app import _version
            out["version"] = _version()
        except Exception:
            pass
        return out

    def sessions(self) -> List[Dict[str, Any]]:
        from agentx_dev.cli.sessions import list_sessions
        current = str(getattr(self.app, "session_path", "") or "")
        try:
            found = list_sessions(self.home, self.cwd)
        except Exception:
            return []
        return [{"id": info.session_id, "title": _text(info.title, 200) or "(no message)",
                 "updated": str(info.updated_at), "current": str(info.path) == current} for info in found[:200]]

    def history(self, upto: Optional[int] = None) -> List[Dict[str, Any]]:
        """The current conversation as items for the window; ``upto``: only its first messages (those it had when
        it was opened: what came later reaches the window as events)."""
        session = getattr(self.app, "session", None)
        messages = list(getattr(session, "history", None) or [])
        if upto is not None:
            messages = messages[:max(0, upto)]
        items: List[Dict[str, Any]] = []
        calls: Dict[str, str] = {}
        last_action = ""
        from agentx_dev.Pictures import content_text
        from agentx_dev.Runner.AgentRun import INTERJECTION_PREFIX
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            text = content_text(message.get("content"))
            if role == "system":
                continue
            if role == "user":
                if text.startswith("Observation: "):            # a text-mode model's tool result
                    result = text[len("Observation: "):]
                    failed = result.lstrip().startswith(("ERROR", "Error:", "Refused", "The user declined"))
                    items.append({"type": "tool_result", "name": last_action, "failed": failed,
                                  "summary": "" if failed else _text(summarize_result(last_action, result)[0], 300),
                                  "detail": "\n".join(result.split("\n")[:_MAX_RESULT_LINES]) if failed else "",
                                  "more": 0})
                    continue
                if text.startswith(INTERJECTION_PREFIX):          # typed while it worked
                    items.append({"type": "echo", "text": _text(text[len(INTERJECTION_PREFIX):], 4000),
                                  "outcome": "read by the agent", "done": True})
                    continue
                for marker in ("\n\n<project-notes>", "\n\n[agentx]", "\n\n[framework]"):
                    text = text.split(marker, 1)[0]
                if text.startswith(("[framework]", "[agentx]")):
                    continue
                items.append({"type": "user", "text": _text(text, 100_000)})
            elif role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    calls[str(call.get("id"))] = name
                    try:
                        from agentx_dev.cli.approval import subject_of
                        subject = subject_of(name, args if isinstance(args, dict) else {})
                    except Exception:
                        subject = ""
                    items.append({"type": "tool_call", "name": name, "subject": _text(subject, 400),
                                  "look": name in HELPER_LOOKS})
                step = _text_mode_step(text) if not message.get("tool_calls") else None
                if step is not None:                               # a text-mode model's call or answer, as JSON
                    name, args = step
                    if name.lower().replace("_", "") in ("finalanswer", "final", "respond"):
                        answer = args.get("answer") if isinstance(args, dict) else args
                        items.append({"type": "final", "text": _text(answer, 200_000)})
                    else:
                        last_action = name
                        try:
                            from agentx_dev.cli.approval import subject_of
                            subject = subject_of(name, args if isinstance(args, dict) else {})
                        except Exception:
                            subject = ""
                        items.append({"type": "tool_call", "name": name, "subject": _text(subject, 400),
                                      "look": name in HELPER_LOOKS})
                elif text.strip():
                    items.append({"type": "final", "text": _text(text, 200_000)})
            elif role == "tool":
                name = calls.get(str(message.get("tool_call_id")), str(message.get("name") or ""))
                failed = text.lstrip().startswith(("ERROR", "Error:", "Refused", "The user declined"))
                summary = "" if failed else summarize_result(name, text)[0]
                items.append({"type": "tool_result", "name": name, "summary": _text(summary, 300), "failed": failed,
                              "detail": "\n".join(text.split("\n")[:_MAX_RESULT_LINES]) if failed else "", "more": 0})
        return items

    def history_payload(self, upto: Optional[int] = None) -> Dict[str, Any]:
        """The conversation so far, the last event it covers, and the questions open now (a reloaded window shows
        them again)."""
        seq = self.bus.last()
        with self._lock:
            pending = [{"type": "ask", "id": ident, "kind": request["kind"],
                        **{k: v for k, v in request.items() if k not in ("kind", "future", "request")}}
                       for ident, request in sorted(self._pending.items())]
        return {"seq": seq, "items": self.history(upto), "pending": pending, "status": self._status()}

    def _status(self) -> Dict[str, Any]:
        app = self.app
        status: Dict[str, Any] = {"working": self._working()}
        if app is None:
            return status
        progress = app.progress
        if status["working"]:
            since = float(getattr(progress, "_since", time.monotonic()))
            if since not in self._since_wall:
                self._since_wall = {since: time.time() - (time.monotonic() - since)}
            status["label"] = _text(getattr(progress, "_label", ""), 200)
            status["startedAt"] = round(self._since_wall[since], 2)
        try:
            status["waiting"] = [{"text": _text(text, 300), "where": where} for text, where in app._waiting_shown()]
        except Exception:
            status["waiting"] = []
        rows = []
        try:
            for step in sorted(list(app._team_rows.values()), key=lambda s: getattr(s, "slot", 0)):
                snapshot = step.snapshot()
                rows.append({"helper": _text(step.helper, 120), "label": _text(step.label, 80),
                             "activity": _text(helper_activity(step_counts(snapshot)), 300),
                             "stats": _text(step_stats(snapshot), 120)})
            done, total = app._team_steps
            status["team"] = {"rows": rows, "done": done, "total": total} if rows else None
        except Exception:
            status["team"] = None
        try:
            status["recording"] = bool(app.recording()) or any(
                request["kind"] == "text" and RECORDING_MARK in str(request.get("prompt", ""))
                for request in list(self._pending.values()))
        except Exception:
            status["recording"] = False
        status["mode"] = getattr(app.gate, "mode", "default")
        try:
            status["model"] = str(app.model_label)
        except Exception:
            pass
        return status

    def _tick(self) -> None:
        while not self._closed:
            try:
                status = self._status()
                encoded = json.dumps(status, sort_keys=True, default=str)
                if encoded != self._last_status:
                    self._last_status = encoded
                    self.bus.emit("status", **status)
                conversation = str(getattr(self.app, "session_path", "") or "") if self.app is not None else ""
                if conversation != self._conversation:
                    self._conversation = conversation
                    session = getattr(self.app, "session", None) if self.app is not None else None
                    self.bus.emit("conversation", path=conversation,
                                  base=len(getattr(session, "history", None) or []))
            except Exception:
                pass
            time.sleep(TICK_SEC)

    # -- the app ------------------------------------------------------------------------------------------------

    def wire(self, app: Any) -> None:
        """Called once the app is built: it draws through the window from now on."""
        from agentx_dev.cli.team import on_main
        self.app = app
        app.fullscreen = True                         # no key reading in the terminal; typed lines come from here
        screen = WebScreen(self.console, self.bus)
        screen.cwd = getattr(app.screen, "cwd", None)
        app.screen = screen
        app.progress.drawn_by = self.bus.wake
        app.insert_input = self.insert
        app.set_submit_empty = lambda on: None
        app._echo = lambda text, outcome, done=True: self.bus.emit(           # a typed line and what became of it
            "echo", text=_text(text, 2000), outcome=_text(outcome, 600), done=bool(done))
        asking = app._remembering(self.approve)
        app.gate.ask = lambda request: on_main(asking, request)

    def address(self) -> str:
        port = self._server.server_address[1] if self._server is not None else self.port
        return f"http://127.0.0.1:{port}/?t={self.token}"

    def start_server(self) -> str:
        front = self

        class Handler(_Handler):
            pass
        Handler.front = front
        self._server = _QuietServer(("127.0.0.1", int(self.port or 0)), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="agentx-ui-server", daemon=True).start()
        threading.Thread(target=self._tick, name="agentx-ui-status", daemon=True).start()
        return self.address()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
        for request in pending:
            if not request["future"].done():
                request["future"].set_exception(EOFError())
        self.bus.emit("closed")
        if self._server is not None:
            threading.Timer(1.0, self._server.shutdown).start()

    def run(self, work: Callable[[], int]) -> int:
        box: Dict[str, Any] = {}

        def worker() -> None:
            try:
                box["code"] = work()
            except BaseException as error:           # noqa: BLE001 - raised again on this thread
                box["error"] = error

        url = self.start_server()
        self._worker = threading.Thread(target=worker, name="agentx-work", daemon=True)
        self._worker.start()
        how = self.opener(url) if self.open_it else ""
        print(f"agentx is open {'in an ' + how if how == 'app window' else 'in your browser' if how else 'at'}: {url}",
              flush=True)
        print("Keep this terminal open; close it or press Ctrl+C here to quit agentx.", flush=True)
        try:
            while self._worker.is_alive():
                self._worker.join(0.5)
        except KeyboardInterrupt:
            self.close()
            self._worker.join(5)
            return 130
        self.close()
        if "error" in box:
            raise box["error"]
        return int(box.get("code") or 0)


class _Handler(BaseHTTPRequestHandler):
    front: WebFront
    protocol_version = "HTTP/1.1"
    server_version = "agentx"

    def log_message(self, format: str, *args: Any) -> None:     # quiet: the terminal shows the address only
        pass

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, query: Dict[str, List[str]]) -> bool:
        given = self.headers.get("X-Agentx-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(str(given), self.front.token)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json.dumps(value, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:                                       # noqa: N802 - the http.server name
        if not self._host_ok():
            return self._json({"error": "wrong host"}, 403)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            try:
                body = (RESOURCES / name).read_bytes()
            except OSError:
                return self._json({"error": "missing"}, 404)
            extra = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                                "style-src 'self' 'unsafe-inline'; connect-src 'self'"} \
                if name == "index.html" else None
            return self._send(200, body, content_type, extra)
        if not self._token_ok(query):
            return self._json({"error": "forbidden"}, 403)
        front = self.front
        if url.path == "/events":
            after = self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0] or "0"
            try:
                return self._events(max(0, int(after)))
            except ValueError:
                return self._events(0)
        if url.path == "/api/state":
            return self._json(front.state())
        if url.path == "/api/sessions":
            return self._json(front.sessions())
        if url.path == "/api/history":
            raw = (query.get("upto") or [""])[0]
            return self._json(front.history_payload(int(raw) if raw.isdigit() else None))
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:                                      # noqa: N802
        if not self._host_ok():
            return self._json({"error": "wrong host"}, 403)
        url = urlparse(self.path)
        if not self._token_ok(parse_qs(url.query)):
            return self._json({"error": "forbidden"}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json({"error": "send JSON"}, 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._json({"error": "too large"}, 413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "bad JSON"}, 400)
        front = self.front
        if url.path == "/api/send":
            return self._json({"result": front.send(str(data.get("text", "")))})
        if url.path == "/api/answer":
            try:
                ident = int(data.get("id"))
            except (TypeError, ValueError):
                return self._json({"error": "no id"}, 400)
            return self._json({"ok": front.answer(ident, data.get("value"))})
        if url.path == "/api/stop":
            return self._json({"result": front.stop()})
        if url.path == "/api/voice":
            return self._json({"result": front.voice()})
        if url.path == "/api/upload":
            return self._json(front.upload(str(data.get("name", "")), str(data.get("data", "")),
                                           str(data.get("type", ""))))
        return self._json({"error": "not found"}, 404)

    def _events(self, after: int) -> None:
        """Server-sent events: everything after ``after``, then each new one as it comes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = after
        try:
            while True:
                events = self.front.bus.after(last, wait=15.0)
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                for event in events:
                    last = max(last, int(event.get("seq", last)))
                    payload = json.dumps(event, default=str)
                    self.wfile.write(f"id: {event.get('seq')}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if any(event.get("type") == "closed" for event in events):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return
