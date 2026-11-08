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
import urllib.error
import urllib.request
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
MODES_SHOWN = ("default", "accept-edits", "plan", "auto")
KEEP_ALIVE_SEC = 4.0                # a closed window is noticed when this write fails
CLOSE_GRACE_SEC = 8.0               # how long the window may be gone (a reload) before agentx quits
SHARE_CHECK_SEC = 30.0              # remote use: how often agentx checks Tailscale still shares it (after a sleep)
ROOM_IDLE_SEC = 15 * 60            # a device's own conversation no window used for this long is closed (and saved)
ROOM_REMOTE_NOTE = ("Remote control is turned on and off on the computer agentx runs on; this device has its own "
                    "conversation there.")
_DEVICE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _device_id(value: Any) -> str:
    """A window's device id (made by the page, kept in the browser), or "" when there is none or it is malformed."""
    text = str(value or "").strip()
    return text if _DEVICE.match(text) else ""
SKIP_FOLDERS = {"node_modules", "__pycache__", "dist", "build", "venv", "env", "target", "out", "coverage"}
_MAX_RESULT_LINES = 12
_MAX_OUTPUT_LINES = 120             # what a step shows when you open it
_MAX_OUTPUT_CHARS = 8000


def _output(raw_text: Any) -> Dict[str, Any]:
    """A tool's output for the step you can open: the first lines, each cut, and how many lines were left out."""
    lines = str(raw_text or "").replace("\r\n", "\n").rstrip("\n").split("\n")
    shown: List[str] = []
    size = 0
    for line in lines[:_MAX_OUTPUT_LINES]:
        line = line[:400]
        if size + len(line) > _MAX_OUTPUT_CHARS:
            break
        shown.append(line)
        size += len(line) + 1
    text = "\n".join(shown)
    if not text.strip():
        return {"output": "", "outputMore": 0}
    return {"output": text, "outputMore": len(lines) - len(shown)}


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
        if kind in ("thought", "reasoning"):         # shown at once: the window folds it into a "Thinking" block
            text = _text(event.get("content")).strip()
            if text:
                self.bus.emit("thought", text=text, reasoning=kind == "reasoning")
            return
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
                          more=max(0, len(lines) - _MAX_RESULT_LINES) if failed else 0,
                          **(_output(raw_text) if not failed else {}))
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


def _text_mode_thought(text: str) -> str:
    """The ``thought`` a text-mode step carries (``{"thought": ..., "action": ...}``), or ""."""
    try:
        data = json.loads(str(text or "").strip())
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("thought", data.get("Thought", ""))
    return value.strip() if isinstance(value, str) else ""


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


def ssh_server_running(port: int = 22, timeout: float = 0.5) -> bool:
    """Whether something accepts connections on this machine's SSH port: the tunnel needs an SSH server here."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def local_addresses() -> List[str]:
    """This machine's IPv4 addresses on its networks (not 127.x or 169.254.x), for the ssh command."""
    import socket
    found: List[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = str(info[4][0])
        if not address.startswith(("127.", "169.254.")) and address not in found:
            found.append(address)
    if not found:                                       # the address a route out would use (nothing is sent)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                address = probe.getsockname()[0]
                if not address.startswith(("127.", "0.")):
                    found.append(address)
        except OSError:
            pass
    # The home or office network first: a Tailscale address (100.x) only answers devices signed in to Tailscale, and
    # those can open the Tailscale link instead. Put first, it made ssh time out from a laptop without Tailscale.
    return sorted(found, key=is_tailnet_ip)


def is_tailnet_ip(address: Any) -> bool:
    """An address Tailscale gives a device (100.64.0.0/10, or its IPv6 range): only your own devices have them."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(str(address).strip("[]"))
    except ValueError:
        return False
    if ip.version == 4:
        return ip in ipaddress.ip_network("100.64.0.0/10")
    return ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")


TAILSCALE_HTTPS_HELP = ("No link for your iPad or phone this time: Tailscale is connected, but HTTPS is off for your "
                        "Tailscale network (it can be turned off in your Tailscale account, also after it worked). "
                        "Turn it on: open https://login.tailscale.com/admin/dns and, under HTTPS Certificates, click "
                        "Enable HTTPS. Then /remote-control off and /remote-control again (or start agentx again).")
TAILSCALE_SERVE_HELP = ("No link for your iPad or phone this time: Tailscale's sharing to your own devices (Serve) is "
                        "off for your Tailscale network. Turn it on once at {link}, then /remote-control off and "
                        "/remote-control again (or start agentx again).")


def _tailscale_exe() -> Optional[str]:
    candidates = [shutil.which("tailscale")]
    if os.name == "nt":
        candidates.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return next((exe for exe in candidates if exe and os.path.isfile(exe)), None)


def _tailscale(args: Sequence[str], timeout: float = 20) -> "tuple[int, str]":
    exe = _tailscale_exe()
    if exe is None:
        return 127, "Tailscale is not installed"
    try:
        done = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:          # what it said before waiting (say, a link to allow Serve)
        said = "".join(part.decode(errors="replace") if isinstance(part, bytes) else part
                       for part in (error.stdout, error.stderr) if part)
        return 124, said.strip() or "Tailscale did not answer in time"
    except OSError as error:
        return 126, str(error)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def tailscale_name() -> "tuple[Optional[str], str]":
    """``(name, problem)``: this computer's Tailscale name (``pc.tailnet.ts.net``) when Tailscale is connected and
    HTTPS is on for the tailnet, else None and what to do ("" when Tailscale is not installed at all)."""
    if _tailscale_exe() is None:
        return None, ""
    code, out = _tailscale(["status", "--json"], timeout=10)
    try:
        data = json.loads(out) if code == 0 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get("BackendState") != "Running":
        return None, "Tailscale is installed but not connected on this computer: open it and sign in, then start again."
    name = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        return None, "Tailscale gave this computer no name (turn on MagicDNS in its admin page), so use SSH below."
    if not data.get("CertDomains"):
        return None, TAILSCALE_HTTPS_HELP
    return name, ""


def tailscale_serve(port: int) -> str:
    """Have Tailscale serve ``https://<this computer's name>:<port>`` to your own devices, passed to agentx on
    127.0.0.1:<port>. Returns "" when it worked, else what went wrong."""
    # When Serve is off for the tailnet, Tailscale prints a link to allow it and then waits: give up after a while
    # and pass that link on.
    code, out = _tailscale(["serve", "--bg", f"--https={int(port)}", f"http://127.0.0.1:{int(port)}"], timeout=12)
    if code == 0:
        return ""
    if "serve is not enabled" in out.lower():
        link = next((word for word in out.split() if word.startswith("https://login.tailscale.com/")),
                    "https://login.tailscale.com/admin/settings/features")
        return TAILSCALE_SERVE_HELP.format(link=link)
    if "https" in out.lower() and ("enable" in out.lower() or "cert" in out.lower()):
        return TAILSCALE_HTTPS_HELP
    return f"Tailscale could not serve agentx ({out.splitlines()[0][:200] if out else f'exit {code}'}); use SSH below."


def tailscale_unserve(port: int) -> None:
    _tailscale(["serve", f"--https={int(port)}", "off"], timeout=10)


def tailscale_agentx_ports() -> Optional[set]:
    """The ports Tailscale shares the way agentx does (``https://<name>:N`` passed to ``http://127.0.0.1:N``), or
    None when Tailscale cannot say."""
    code, out = _tailscale(["serve", "status", "--json"], timeout=10)
    if code != 0:
        return None
    try:
        data = json.loads(out or "{}") or {}
    except ValueError:
        return None
    ports = set()
    for site, config in ((data.get("Web") or {}) if isinstance(data, dict) else {}).items():
        port = str(site).rpartition(":")[2]
        proxy = (((config or {}).get("Handlers") or {}).get("/") or {}).get("Proxy", "")
        if port.isdigit() and proxy == f"http://127.0.0.1:{port}":
            ports.add(int(port))
    return ports


def _listening(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def tailscale_clear_leftovers(keep: Optional[int] = None) -> List[int]:
    """Take away what earlier agentx runs left shared (a run that was killed, or a computer that slept through it):
    agentx-style entries whose port nothing listens on any more. Returns the ports cleared."""
    cleared = []
    for port in sorted(tailscale_agentx_ports() or ()):
        if port != keep and not _listening(port):
            tailscale_unserve(port)
            cleared.append(port)
    return cleared


def windows_ssh_blocked() -> str:
    """On Windows: a line saying why other computers cannot reach SSH here, when the network is set to Public and
    the OpenSSH firewall rule only lets SSH in on Private networks (the usual case at home); else ""."""
    if os.name != "nt":
        return ""
    script = ("$c = @(Get-NetConnectionProfile | ForEach-Object { [string]$_.NetworkCategory }) -join ',';"
              "$r = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue;"
              "$p = if ($r -and $r.Enabled -eq 'True') { [string]$r.Profile } else { 'none' };"
              "Write-Output \"$c|$p\"")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    categories, _, profile = (done.stdout or "").strip().rpartition("|")
    if not categories:
        return ""
    if profile == "none":
        return ("Windows Firewall has no enabled rule letting SSH in (OpenSSH-Server-In-TCP), so other computers "
                "time out.")
    if "Public" in categories.split(",") and not any(p in profile for p in ("Public", "Any")):
        return ("This computer's network is set to Public, and Windows Firewall lets SSH in only on Private "
                "networks, so other computers time out. At home, set it to Private (Settings > Network & internet > "
                "Wi-Fi > your network > Private network), or in PowerShell as administrator:\n"
                "    Set-NetConnectionProfile -InterfaceAlias \"Wi-Fi\" -NetworkCategory Private")
    return ""


ENABLE_SSH_SERVER = {
    "nt": ("On Windows, in PowerShell opened as administrator (once; it also opens the firewall for SSH):\n"
           "    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0\n"
           "    Start-Service sshd\n"
           "    Set-Service -Name sshd -StartupType Automatic\n"
           "You sign in over SSH with your Windows user name and password (for a Microsoft account, its password)."),
    "darwin": "On a Mac: System Settings > General > Sharing > Remote Login, on.",
    "posix": "On Linux: install and start the OpenSSH server, for example\n"
             "    sudo apt install openssh-server && sudo systemctl enable --now ssh",
}


def remote_instructions(url: str, ssh_running: Optional[bool] = None,
                        addresses: Optional[Sequence[str]] = None, tailnet_url: Optional[str] = None,
                        tailnet_problem: str = "", firewall: Optional[str] = None) -> str:
    """``agentx --ui --remote``: the window stays on this machine's 127.0.0.1 (never on the network); the person
    reaches it through an SSH tunnel from their own computer. This says how, with this machine's names filled in, and
    says first when no SSH server runs here (then the ssh command is refused)."""
    import getpass
    import socket
    parsed = urlparse(url)
    port = parsed.port or 0
    try:
        user = getpass.getuser()
    except Exception:
        user = "you"
    host = socket.gethostname() or "this-server"
    running = ssh_server_running() if ssh_running is None else ssh_running
    found = sorted(local_addresses() if addresses is None else addresses, key=is_tailnet_ip)
    target = found[0] if found else host
    nearby = [a for a in found[1:] if not is_tailnet_ip(a)]
    tailnet_ips = [a for a in found if is_tailnet_ip(a)]
    others = f" (or {', '.join(nearby + [host])})" if found else ""
    if tailnet_url:
        lines = [f"agentx is running here, on {host}, for use from your other devices.",
                 "Nothing more to do on this computer: leave this terminal open (and the computer awake).", "",
                 "On your iPad, phone or another computer signed in to the same Tailscale account, open:", "",
                 f"       {tailnet_url}", "",
                 "Only your own Tailscale devices can reach it, wherever they are; it carries this run's secret key, so do",
                 "not share it. Nothing is opened on your Wi-Fi or the internet.", "",
                 "Or, from a computer without Tailscale, through SSH:", ""]
    elif tailnet_problem:                  # Tailscale is here but gave no link: say that first, not under the SSH way
        lines = [tailnet_problem, "",
                 f"Meanwhile agentx is running here, on {host}, and only an SSH connection to this computer reaches it.",
                 "Leave this terminal open (and the computer awake).", ""]
    else:
        lines = [f"agentx is running here, on {host}, for use from another computer. It is not on the network: only an",
                 "SSH connection to this computer reaches it.",
                 "Nothing more to do on this computer: leave this terminal open (and the computer awake).", "",
                 "From an iPad or phone: install Tailscale on this computer and on it, sign both in to the same account, "
                 "then start agentx --remote again; it prints a link to open there.",
                 ""]
    if not running:
        key = "nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "posix"
        lines += ["First, once: no SSH server runs on this computer, so the other computer cannot connect yet.",
                  ENABLE_SSH_SERVER[key], ""]
    else:
        blocked = windows_ssh_blocked() if firewall is None else firewall
        if blocked:
            lines += ["First: " + blocked, ""]
    lines += ["On the other computer (the one you use the window from):", "",
              "  1. Run this, and leave it open. It prints nothing while it works; that silence is the connection:", "",
              f"       ssh -N -L {port}:127.0.0.1:{port} {user}@{target}", "",
              "  2. Open this in that computer's browser (it carries this run's secret key; do not share it):", "",
              f"       {url}", "",
              f"     It says 127.0.0.1 because step 1 makes that computer's port {port} lead here.", "",
              *([f"{target} is this computer's address on this network{others}. Away from this network it does not "
                 "reach here:",
                 (f"from a computer with Tailscale on, use {tailnet_ips[0]} (its Tailscale address) in step 1, from "
                  "anywhere." if tailnet_ips else
                  "use a private network such as Tailscale on both computers, and its address for this one in step 1.")]
                if not is_tailnet_ip(target) else
                [f"{target} is this computer's Tailscale address: it answers only computers signed in to your Tailscale."]),
              f"The port changes with each start; add --port {port} to keep this one. Port {port} busy on the other "
              f"computer? Use",
              f"ssh -N -L 9000:127.0.0.1:{port} ... and open http://127.0.0.1:9000/?t=... instead.",
              f"(Sitting at this computer instead? The same address works here directly, without ssh.)"]
    return "\n".join(lines)


class WebFront:
    """The window's side of agentx: ``console``, ``read``, ``read_short`` and ``select`` for the app, ``wire(app)``
    once it is built, and ``run(work)``, which serves the window while ``work`` (the app) runs on a worker thread."""

    def __init__(self, *, cwd: Path, home: Path, port: int = 0, open_it: bool = True,
                 opener: Callable[[str], str] = open_window, remote: bool = False) -> None:
        self.cwd = Path(cwd)
        self.home = Path(home)
        self.port = port
        self.open_it = open_it and not remote
        self.remote = remote
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
        self._files: Optional[tuple] = None                 # (when, the project's files) for "Mention a file"
        self.tailnet: Optional[str] = None                  # --remote: this computer's Tailscale address, when on
        self.tailnet_problem = ""
        self._serving: Optional[int] = None                # the port Tailscale serves as https, while it does
        self._handler: Any = None
        self.on_window: Optional[Callable[[str], None]] = None   # where "a window connected" goes (default: print)
        self._windows = 0                                   # windows connected now (their event streams)
        self._had_window = False
        self._alone_since: Optional[float] = None
        self._last_status = ""
        self._since_wall: Dict[float, float] = {}
        self._conversation = ""
        self.console = Console(file=_Sink(self._printed), force_terminal=True, color_system="truecolor",
                               width=CONSOLE_WIDTH, legacy_windows=False)
        # One conversation per device. The front that serves the window (the "host") has the main conversation;
        # every other device gets a room of its own: another WebFront (own events, questions, Stop and status) with
        # its own ChatApp, built by ``make_device_app(room)``. Without that factory every window shares the main one.
        self.host: Optional["WebFront"] = None              # on a room: the front that serves the window
        self.device = ""                                     # on a room: the device it belongs to
        self.rooms: Dict[str, "WebFront"] = {}
        self.joined: set = set()                             # devices that opened the main conversation instead
        self.main_owner: Optional[str] = None
        self.first_device_owns_main = True                   # --ui: the first device to connect has the main one
        self.make_device_app: Optional[Callable[["WebFront"], Any]] = None
        self.last_session: Dict[str, str] = {}               # a room closed when unused reopens its conversation
        self.ended = False
        self.idle = False

    # -- what the app prints and asks ----------------------------------------------------------------------------

    def _printed(self, text: str) -> None:
        with self._lock:
            data = self._partial + text.replace("\r\n", "\n")
            done, sep, self._partial = data.rpartition("\n")
        if sep and done.strip():
            self.bus.emit("print", ansi=done)

    def open_request(self, kind: str, **data: Any) -> "tuple[int, Future]":
        """Show a question in the window (a card, or the input box for ``message``) without waiting for it: the id
        and the future its answer sets. ``close_request`` takes it away."""
        with self._lock:
            if self._closed:
                raise EOFError()
            self._ids += 1
            ident = self._ids
            future: Future = Future()
            self._pending[ident] = {"kind": kind, "future": future, **data}
        self.bus.emit("ask", id=ident, kind=kind, **{k: v for k, v in data.items() if k != "request"})
        return ident, future

    def close_request(self, ident: int, outcome: str = "") -> None:
        with self._lock:
            self._pending.pop(ident, None)
        self.bus.emit("answered", id=ident, **({"outcome": _text(outcome, 300)} if outcome else {}))

    def _ask(self, kind: str, **data: Any) -> Any:
        ident, future = self.open_request(kind, **data)
        try:
            return future.result()
        finally:
            self.close_request(ident)

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
        fields = self.approval_fields(request)
        if isinstance(fields, Answer):
            return fields
        value = self._ask("approval", **fields)
        if not isinstance(value, dict):
            return Answer("n", "")
        choice = str(value.get("choice") or "n")
        if choice == "a" and not fields["always"]:
            choice = "y"
        return Answer(choice if choice in ("y", "a", "n") else "n", _text(value.get("reason"), 2000).strip())

    def approval_fields(self, request: Any) -> Any:
        """What an approval card shows, or the ``Answer`` when it is declined unseen (too long to preview)."""
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
        return {"tool": _text(request.tool, 80), "title": _text(title, 2000),
                "subject": _text(getattr(request, "subject", ""), 2000), "body": _text(body, 400_000),
                "body_kind": str(kind), "helper": _text(helper, 120), "always": always, "grant": grant}

    def insert(self, words: str) -> bool:
        self.bus.emit("insert", text=_text(words, 20_000))
        return True

    # -- MCP sign-in from another device -----------------------------------------------------------------------

    def offer_signin(self, url: str, server: str = "") -> bool:
        """In remote use: show an MCP server's sign-in page as a card in the window instead of opening a browser on
        this computer (nobody is at it). False when not in remote use: this computer's browser opens it."""
        if not self.remote or not str(url).lower().startswith(("https://", "http://")):
            return False
        self.bus.emit("signin", url=_text(url, 4000), server=_text(server, 60))
        return True

    def finish_signin(self, pasted: str) -> Dict[str, Any]:
        """The address the browser could not open after signing in (``http://localhost:<port>/callback?code=...``):
        its code is handed to the sign-in agentx waits for on this computer, as if the browser had reached it."""
        from agentx_dev.cli.mcp import CALLBACK_PORT
        parsed = urlparse(str(pasted or "").strip())
        query = parse_qs(parsed.query)
        if parsed.path.rstrip("/") != "/callback" or not (query.get("code") or query.get("error")):
            return {"ok": False, "message": "That is not the sign-in address: copy the whole address of the page that "
                                            "could not load (it starts with http://localhost and has code= in it)."}
        if parsed.hostname not in ("localhost", "127.0.0.1") or (parsed.port and parsed.port != CALLBACK_PORT):
            return {"ok": False, "message": "That address is not agentx's sign-in address on this computer."}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CALLBACK_PORT}/callback?{parsed.query}", timeout=15):
                pass
        except urllib.error.HTTPError:
            pass                                        # the waiting page answered; the sign-in reads the code
        except OSError:
            return {"ok": False, "message": "No sign-in is waiting on this computer any more (it waits 5 minutes): "
                                            "start it again with the Sign in button."}
        return {"ok": True, "message": "Signed in: agentx is finishing the connection."}

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
        """The spinner runs: drawn by a screen (``_shown``: the window, full-screen mode) or by the terminal itself
        (``_live``: the normal screen, which /remote-control mirrors)."""
        progress = getattr(self.app, "progress", None)
        return bool(progress is not None and (getattr(progress, "_shown", False)
                                              or getattr(progress, "_live", None) is not None))

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

    # -- the side panel: context, tokens, team work, MCP servers, memory ------------------------------------------

    def details(self) -> Dict[str, Any]:
        app = self.app
        out: Dict[str, Any] = {"context": None, "usage": None, "team": None, "mcp": [], "mcpProblems": [],
                               "memory": None}
        if app is None:
            return out
        try:
            from agentx_dev.cli.compact import THRESHOLD_CHARS, history_size
            from agentx_dev.Runner.Trim import TrimPolicy, trim_tool_results
            history = list(getattr(getattr(app, "session", None), "history", None) or [])
            out["context"] = {"chars": history_size(trim_tool_results(history, TrimPolicy())),
                              "limit": THRESHOLD_CHARS, "messages": len(history)}
        except Exception:
            pass
        try:
            requests, tokens_in, tokens_out, cached = app._usage_totals()
            last = getattr(getattr(app, "model", None), "usage", None)
            out["usage"] = {"requests": requests, "input": tokens_in, "output": tokens_out, "cached": cached,
                            "lastInput": int(getattr(last, "last_input_tokens", 0) or 0),
                            "lastTurn": list(getattr(app, "_last_turn_usage", None) or []),
                            "tokenLimit": int(getattr(app, "token_limit", 0) or 0),
                            "maxSteps": int(getattr(app, "max_steps", 0) or 0)}
        except Exception:
            pass
        out["team"] = self._team_details()
        manager = getattr(app, "mcp", None)
        if manager is not None:
            for name, config in sorted(getattr(manager, "servers", {}).items()):
                status = str(getattr(manager, "status", {}).get(name, "") or "not started")
                low = status.lower()
                state = ("on" if low.startswith("connected") else "signin" if "sign-in" in low or "sign in" in low
                         else "off" if low in ("signed out", "disconnected", "not started") else "failed")
                tools = re.search(r"(\d+) tools?", status)
                out["mcp"].append({"name": name, "transport": getattr(config, "transport", ""),
                                   "target": _text(getattr(config, "url", "") or getattr(config, "command", ""), 200),
                                   "status": _text(status, 300), "state": state,
                                   "tools": int(tools.group(1)) if tools else None})
            out["mcpProblems"] = [_text(p, 300) for p in getattr(manager, "problems", []) or []]
        try:
            from agentx_dev.cli.context import find_context_files
            files = []
            for path in find_context_files(self.cwd, self.home):
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                files.append({"path": self._relative(str(path)), "bytes": size})
            memory = getattr(app, "project_memory", None)
            log = self.cwd / ".agentx" / "log.md"
            out["memory"] = {"files": files, "notes": len(memory.notes()) if memory is not None else None,
                             "reports": len(memory.team_reports()) if memory is not None else None,
                             "log": log.is_file()}
        except Exception:
            pass
        return out

    def _relative(self, path: str) -> str:
        try:
            return Path(path).resolve().relative_to(self.cwd.resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    def _team_details(self) -> Optional[Dict[str, Any]]:
        """The last team job: each step, its numbers and what it did (newest steps last)."""
        app = self.app
        steps = list(getattr(app, "_team_log", None) or [])
        if not steps:
            return None
        try:
            from agentx_dev.cli.approval import subject_of
        except Exception:                                   # pragma: no cover - the module is always there
            subject_of = None                               # type: ignore[assignment]
        shown = []
        for step in steps[-12:]:
            try:
                snap = step.snapshot()
            except Exception:
                continue
            actions = []
            for entry in snap.get("log", [])[-40:]:
                name, args = (entry[0], entry[1]) if isinstance(entry, (list, tuple)) and len(entry) > 1 else (entry, {})
                subject = ""
                if subject_of is not None:
                    try:
                        subject = self._relative(subject_of(str(name), args if isinstance(args, dict) else {}))
                    except Exception:
                        subject = ""
                failed = bool(entry[3]) if isinstance(entry, (list, tuple)) and len(entry) > 3 else False
                actions.append({"name": _text(name, 80), "subject": _text(subject, 200), "failed": failed})
            shown.append({"helper": _text(getattr(step, "helper", ""), 80), "label": _text(getattr(step, "label", ""), 120),
                          "query": _text(getattr(step, "query", ""), 600), "done": getattr(step, "ended", None) is not None,
                          "seconds": round(float(snap.get("seconds") or 0)), "tokens": int(snap.get("tokens") or 0),
                          "toolUses": int(snap.get("tool_uses") or 0), "files": len(snap.get("files") or {}),
                          "actions": actions})
        return {"steps": shown, "running": bool(getattr(app, "_team_rows", None))}

    # -- the Settings panel ------------------------------------------------------------------------------------

    def settings_view(self) -> Dict[str, Any]:
        from agentx_dev.cli.settings import is_project_folder, load_settings, project_settings_path, user_settings_path

        def read(path: Path) -> Dict[str, Any]:
            entry: Dict[str, Any] = {"path": str(path), "exists": path.is_file(), "values": {}, "error": ""}
            if not entry["exists"]:
                return entry
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                entry["error"] = "not valid JSON"
                return entry
            if not isinstance(data, dict):
                entry["error"] = "not a JSON object"
                return entry
            permissions = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
            entry["values"] = {"model": data.get("model"), "mode": data.get("mode"),
                               "allow": [str(r) for r in permissions.get("allow") or [] if isinstance(r, str)],
                               "deny": [str(r) for r in permissions.get("deny") or [] if isinstance(r, str)],
                               "maxSteps": data.get("maxSteps"), "tokenLimit": data.get("tokenLimit")}
            return entry

        effective = load_settings(self.cwd, self.home)
        return {"user": read(user_settings_path(self.home)), "folder": read(project_settings_path(self.cwd)),
                "canFolder": is_project_folder(self.cwd, self.home),
                "effective": {"model": effective.model, "mode": effective.mode, "allow": effective.allow,
                              "deny": effective.deny, "maxSteps": effective.max_steps,
                              "tokenLimit": effective.token_limit},
                "problems": [_text(p, 300) for p in effective.problems], "modes": list(MODES_SHOWN)}

    def save_settings(self, scope: str, values: Any) -> Dict[str, Any]:
        """Save the Settings panel for ``scope`` ("user": every folder, "folder": this one). The permission rules,
        step limit and token limit take effect at once; the model and mode are defaults for the next start (the
        menus under the message box change the current conversation)."""
        from agentx_dev.cli.app import default_deny
        from agentx_dev.cli.settings import MAX_MAX_STEPS, is_project_folder, load_settings, write_settings

        if scope not in ("user", "folder") or not isinstance(values, dict):
            return {"ok": False, "message": "Nothing to save."}
        if scope == "folder" and not is_project_folder(self.cwd, self.home):
            return {"ok": False, "message": "This folder cannot have its own settings (it is your home folder or a drive root)."}
        changes: Dict[str, Any] = {}
        if "model" in values:
            model = str(values.get("model") or "").strip()
            changes["model"] = model[:200] or None
        if "mode" in values:
            mode = str(values.get("mode") or "").strip()
            if mode and mode not in MODES_SHOWN:
                return {"ok": False, "message": f"Unknown mode {mode}."}
            if scope == "folder" and mode in ("accept-edits", "auto"):
                return {"ok": False, "message": "A folder's own settings cannot turn on accept-edits or auto; "
                                                "save it for all folders, or use the mode menu for this conversation."}
            changes["mode"] = mode or None
        for key in ("allow", "deny"):
            if key in values:
                rules = values.get(key)
                if not isinstance(rules, list) or len(rules) > 200:
                    return {"ok": False, "message": f"The {key} rules must be a list (at most 200)."}
                cleaned = [str(r).strip()[:300] for r in rules if isinstance(r, str) and str(r).strip()]
                changes[key] = cleaned
        for key, low, high in (("maxSteps", 1, MAX_MAX_STEPS), ("tokenLimit", 0, 50_000_000)):
            if key in values:
                raw = values.get(key)
                if raw in (None, ""):
                    changes[key] = None
                    continue
                try:
                    number = int(raw)
                except (TypeError, ValueError):
                    return {"ok": False, "message": f"{key} must be a whole number."}
                if not low <= number <= high:
                    return {"ok": False, "message": f"{key} must be between {low:,} and {high:,}."}
                changes[key] = number
        path, message = write_settings(self.home if scope == "user" else self.cwd, changes)
        if path is None:
            return {"ok": False, "message": message}
        apps = self._all_apps()                             # every device's conversation in this folder
        if apps:                                            # what can change mid-conversation does, at once
            effective = load_settings(self.cwd, self.home)
            for app in apps:
                try:
                    app.gate.allow = list(effective.allow)
                    app.gate.deny = [*default_deny(self.home), *effective.deny]
                    app.max_steps = max(1, int(effective.max_steps))
                    app.token_limit = int(effective.token_limit)
                except Exception:
                    pass
        return {"ok": True, "message": message, "view": self.settings_view()}

    # -- files to mention, and another folder ------------------------------------------------------------------

    def find_files(self, query: str) -> List[str]:
        now = time.monotonic()
        if self._files is None or now - self._files[0] > 15:
            found: List[str] = []
            root = self.cwd
            for folder, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_FOLDERS and not d.startswith("."))
                for name in sorted(names):
                    try:
                        found.append((Path(folder) / name).relative_to(root).as_posix())
                    except ValueError:
                        continue
                if len(found) >= 20_000:
                    break
            self._files = (now, found)
        words = str(query or "").lower().split()
        hits = [p for p in self._files[1] if all(w in p.lower() for w in words)]
        hits.sort(key=lambda p: (not words or not p.lower().rsplit("/", 1)[-1].startswith(words[-1]), len(p)))
        return hits[:40]

    def open_folder(self, raw: str) -> Dict[str, Any]:
        """Start agentx in another folder, in a window of its own (``agentx --ui`` there). This one keeps running."""
        text = str(raw or "").strip().strip('"')
        if not text:
            return {"ok": False, "message": "Type a folder."}
        folder = Path(text).expanduser()
        if not folder.is_absolute():
            folder = self.cwd / folder
        try:
            folder = folder.resolve()
        except OSError:
            return {"ok": False, "message": f"{text} could not be read."}
        if not folder.is_dir():
            return {"ok": False, "message": f"{folder} is not a folder."}
        if folder == self.cwd.resolve():
            return {"ok": False, "message": "agentx is already open in this folder."}
        command = [sys.executable, "-m", "agentx_dev", "--ui"]
        try:
            if os.name == "nt":
                subprocess.Popen(command, cwd=str(folder), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            else:
                subprocess.Popen(command, cwd=str(folder), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            return {"ok": False, "message": f"agentx could not be started there: {error}"}
        return {"ok": True, "message": f"Opening agentx in {folder}. It asks whether you trust the folder the first time.",
                "folder": str(folder)}

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
                                  "more": 0, **(_output(result) if not failed else {})})
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
                if message.get("tool_calls") and text.strip():     # what it said before its steps: a note
                    items.append({"type": "thought", "text": _text(text.strip(), 20_000)})
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
                    note = _text_mode_thought(text)
                    if note:
                        items.append({"type": "thought", "text": _text(note, 20_000)})
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
                              "detail": "\n".join(text.split("\n")[:_MAX_RESULT_LINES]) if failed else "", "more": 0,
                              **(_output(text) if not failed else {})})
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
            try:                                       # the tokens this message has used so far
                from agentx_dev.cli.team import counted_tokens
                _, tokens_in, tokens_out, cached = app._usage_totals()
                status["tokens"] = max(0, counted_tokens(tokens_in, tokens_out, cached)
                                       - int(getattr(app, "_turn_start", 0) or 0))
            except Exception:
                pass
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
        status["othersWorking"] = self._others_working()
        status["mode"] = getattr(app.gate, "mode", "default")
        try:
            status["model"] = str(app.model_label)
        except Exception:
            pass
        return status

    def _tick(self) -> None:
        while not self._closed:
            if self.host is None and self.rooms:
                try:
                    self._sweep_rooms()
                except Exception:
                    pass
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

    # -- one conversation per device ------------------------------------------------------------------------------

    def route(self, device: str) -> "WebFront":
        """The front a device's requests go to: the main conversation for the device that has it (the first one to
        connect in ``--ui``) and for devices that joined it; else that device's own room, made on its first request."""
        if self.host is not None:
            return self
        device = _device_id(device)
        if not device or self.make_device_app is None:
            return self
        with self._lock:
            if device in self.joined:
                return self
            if self.main_owner is None and self.first_device_owns_main:
                self.main_owner = device
            if device == self.main_owner:
                return self
            room = self.rooms.get(device)
            if room is not None and not room.ended:
                return room
            if self._closed:
                return self
            room = WebFront(cwd=self.cwd, home=self.home, open_it=False, remote=self.remote)
            room.host, room.device, room.token, room.on_window = self, device, self.token, self.on_window
            room._alone_since = time.monotonic()           # closed when no window comes (see _sweep_rooms)
            self.rooms[device] = room
        room._begin(self.last_session.pop(device, None))
        return room

    def _begin(self, resume: Optional[str]) -> None:
        """A room: its status thread, and its own app on a thread of its own (the device's conversation)."""
        host = self.host
        threading.Thread(target=self._tick, name="agentx-ui-status", daemon=True).start()

        def run() -> None:
            try:
                app = host.make_device_app(self)
                self.wire(app)
                app.start(resume)
            except BaseException as error:              # noqa: BLE001 - this device's conversation only
                if not isinstance(error, (EOFError, KeyboardInterrupt, SystemExit)):
                    self.bus.emit("notice", level="error",
                                  text=f"This conversation stopped ({type(error).__name__}): {_text(error, 300)}")
            finally:
                host._room_ended(self)
        self._worker = threading.Thread(target=run, name=f"agentx-device-{self.device[:8]}", daemon=True)
        self._worker.start()

    def _room_ended(self, room: "WebFront") -> None:
        with self._lock:
            if self.rooms.get(room.device) is room:
                del self.rooms[room.device]
            if room.idle:                                   # closed because unused: the device comes back to it
                path = str(getattr(room.app, "session_path", "") or "")
                if path:
                    self.last_session[room.device] = Path(path).name      # find_session takes the file name
        room.ended = True
        room.close()

    def _sweep_rooms(self) -> None:
        """A room no window has used for ``ROOM_IDLE_SEC`` and that is not working is closed (its MCP servers and
        model client go); its conversation is saved, and the device reopens it when it comes back."""
        now = time.monotonic()
        with self._lock:
            rooms = list(self.rooms.values())
        for room in rooms:
            with room._lock:
                unused = (room._windows == 0 and room._alone_since is not None
                          and now - room._alone_since >= ROOM_IDLE_SEC)
                asking = any(r["kind"] != "message" for r in room._pending.values())
            if unused and not asking and not room._working() and not room.ended:
                room.idle = True
                room.close()

    def join(self, device: str, live: bool, then: str = "") -> Dict[str, Any]:
        """A device opens the main conversation (shared with whoever has it) or goes back to its own; going back,
        ``then`` is "resume:<id>" (open that conversation there) or "new" (start a new one there)."""
        device = _device_id(device)
        if not device or self.host is not None:
            return {"ok": False}
        with self._lock:
            if live and self.app is not None and device != self.main_owner:
                self.joined.add(device)
                return {"ok": True}
            self.joined.discard(device)
            room = self.rooms.get(device)
            wanted, session = str(then or ""), ""
            ready = room is not None and not room.ended and room.app is not None
            if wanted.startswith("resume:"):
                session = re.sub(r"[^A-Za-z0-9_.-]", "", wanted[len("resume:"):])[:120]
                if session and not ready:
                    self.last_session[device] = session       # its own conversation starts on that one
        if wanted.startswith("resume:") and ready and session:
            room.send(f"/resume {session}")
        elif wanted == "new" and ready:
            room.send("/clear")
        return {"ok": True}

    def live_info(self, device: str) -> Dict[str, Any]:
        """For a device's list: whether it can open the main conversation, and whether it has."""
        device = _device_id(device)
        available = bool(self.make_device_app is not None and self.app is not None and device
                         and device != self.main_owner)
        title = ""
        if available:
            for item in self.sessions():
                if item.get("current"):
                    title = item["title"]
                    break
        return {"available": available, "joined": device in self.joined,
                "where": "the first window" if self.first_device_owns_main else "the computer",
                "title": title, "working": self._working() if available else False}

    def _others_working(self) -> int:
        """How many other conversations in this folder (the main one, other devices') are working right now."""
        host = self.host or self
        with host._lock:
            fronts = [host, *host.rooms.values()]
        return sum(1 for front in fronts if front is not self and front.app is not None and not front.ended
                   and front._working())

    def _all_apps(self) -> List[Any]:
        host = self.host or self
        with host._lock:
            rooms = list(host.rooms.values())
        return [app for app in [host.app, *(room.app for room in rooms)] if app is not None]

    # -- the app ------------------------------------------------------------------------------------------------

    def wire(self, app: Any) -> None:
        """Called once the app is built: it draws through the window from now on."""
        from agentx_dev.cli.team import on_main
        self.app = app
        app.web_front = self                          # /remote-control adds the remote ways to this window
        app.remote_signin = self.offer_signin         # in remote use, an MCP sign-in page is a card here
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
        app.gate.ask = lambda request: on_main(asking, request, lane=getattr(app, "team_lane", None))

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
        self._handler = Handler
        if self.tailnet:                                # --remote with Tailscale: Tailscale serves it as https
            self._start_tailnet()
        threading.Thread(target=self._tick, name="agentx-ui-status", daemon=True).start()
        return self.address()

    def _start_tailnet(self) -> None:
        """Tailscale serves ``https://<name>:<port>`` to your own devices and passes it to this server on
        127.0.0.1 (agentx itself never listens on another address; Tailscale handles the firewall and HTTPS)."""
        port = self._server.server_address[1]
        try:
            tailscale_clear_leftovers(keep=port)
        except Exception:                               # tidying up must never stop remote use
            pass
        problem = tailscale_serve(port)
        if problem:
            self.tailnet_problem, self.tailnet = problem, None
            return
        self._serving = port
        threading.Thread(target=self._keep_shared, args=(port,), name="agentx-tailscale", daemon=True).start()

    def _keep_shared(self, port: int) -> None:
        """While remote use is on: Tailscale must still share this port. After the computer slept, or Tailscale
        restarted, it can stop doing so and the iPad's link goes dead; then share it again (the same link)."""
        while not self._closed and self._serving == port:
            time.sleep(SHARE_CHECK_SEC)
            if self._closed or self._serving != port:
                return
            try:
                shared = tailscale_agentx_ports()
                if shared is None or port in shared:
                    continue
                problem = tailscale_serve(port)
                stamp = datetime.now().strftime("%H:%M:%S")
                line = (f"[{stamp}] Tailscale had stopped sharing agentx (the computer slept, or Tailscale "
                        f"restarted); shared again: the same link works." if not problem else
                        f"[{stamp}] Tailscale stopped sharing agentx and could not share it again: {problem}")
                (self.on_window or (lambda text: print(text, flush=True)))(line)
            except Exception:
                pass

    def _stop_tailnet(self) -> None:
        port, self._serving = self._serving, None
        if port:
            tailscale_unserve(port)

    def enable_remote(self) -> str:
        """``/remote-control`` in the app window: the window already served here can also be opened from your iPad,
        phone or another computer (Tailscale, or SSH). Closing this window no longer quits agentx while it is on."""
        if self.host is not None:
            return ROOM_REMOTE_NOTE
        if not self.remote:
            self._open_it_before = self.open_it
            self.remote, self.open_it = True, False
            self.tailnet, self.tailnet_problem = tailscale_name()
            if self.tailnet and self._server is not None:
                self._start_tailnet()
        text = remote_instructions(self.address(), tailnet_url=self.tailnet_address(),
                                   tailnet_problem=self.tailnet_problem)
        print(text, flush=True)                         # the terminal agentx runs in shows it too
        return text + "\n\nWhile remote control is on, closing this window does not quit agentx. /remote-control off ends it."

    def disable_remote(self) -> str:
        if self.host is not None:
            return ROOM_REMOTE_NOTE
        if not self.remote:
            return "Remote control is not on."
        self.remote = False
        self.open_it = getattr(self, "_open_it_before", self.open_it)
        self._stop_tailnet()
        self.tailnet = None
        return "Remote control is off: the Tailscale link no longer works. An SSH tunnel to this computer still would."

    def tailnet_address(self) -> Optional[str]:
        if not self.tailnet or not self._serving:
            return None
        return f"https://{self.tailnet}:{self._serving}/?t={self.token}"

    def window_connected(self, on: bool, client: str = "") -> None:
        with self._lock:
            self._windows = max(0, self._windows + (1 if on else -1))
            count = self._windows
            if on:
                self._had_window = True
                self._alone_since = None
            elif self._windows == 0:
                self._alone_since = time.monotonic()
        if self.remote and not self._closed:            # feedback in the terminal: someone is (or is no longer) in
            where = ("through Tailscale (" + client[len("tailscale:"):] + ")" if client.startswith("tailscale:")
                     else "from " + client + " (Tailscale)" if is_tailnet_ip(client)
                     else "through SSH, or on this computer" if client.startswith("127.") or client in ("::1", "")
                     else "from " + client)
            stamp = datetime.now().strftime("%H:%M:%S")
            whose = " (its own conversation)" if self.host is not None else ""
            line = (f"[{stamp}] A window connected {where}{whose}. Open now: {count}." if on
                    else f"[{stamp}] A window closed {where}{whose}. Open now: {count}.")
            try:
                (self.on_window or (lambda text: print(text, flush=True)))(line)
            except Exception:
                pass

    def window_closed(self) -> bool:
        """True once the window agentx opened has been gone for ``CLOSE_GRACE_SEC`` (a reload comes back sooner).
        Only for a window agentx opened itself: with ``--no-open`` or ``--remote`` it keeps running for a later
        connection."""
        if not self.open_it:
            return False
        with self._lock:
            return (self._had_window and self._windows == 0 and self._alone_since is not None
                    and time.monotonic() - self._alone_since >= CLOSE_GRACE_SEC)

    def quit_for_closed_window(self) -> None:
        """The window was closed: stop what runs (kept as a stopped turn, as with Ctrl+C), then end the app."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._worker is not None and self._worker.is_alive():
            if self.stop() == "idle":
                break
            time.sleep(0.3)
        self.close()

    def close(self) -> None:
        with self._lock:
            already = self._closed
            self._closed = True
            pending = list(self._pending.values())
            rooms = list(self.rooms.values())
        for request in pending:
            if not request["future"].done():
                request["future"].set_exception(EOFError())
        if self.host is not None:                       # a device's own conversation ends; the window stays served
            if not already:
                self.bus.emit("closed", room=True, idle=self.idle)
            return
        for room in rooms:
            room.close()
        self.bus.emit("closed")
        self._stop_tailnet()                            # the https link Tailscale served goes away with agentx
        if self._server is not None:
            threading.Timer(1.0, self._server.shutdown).start()

    def run(self, work: Callable[[], int]) -> int:
        box: Dict[str, Any] = {}

        def worker() -> None:
            try:
                box["code"] = work()
            except BaseException as error:           # noqa: BLE001 - raised again on this thread
                box["error"] = error

        if self.remote and self.tailnet is None:
            self.tailnet, self.tailnet_problem = tailscale_name()
        url = self.start_server()
        self._worker = threading.Thread(target=worker, name="agentx-work", daemon=True)
        self._worker.start()
        if self.remote:
            print(remote_instructions(url, tailnet_url=self.tailnet_address(), tailnet_problem=self.tailnet_problem),
                  flush=True)
        else:
            how = self.opener(url) if self.open_it else ""
            print(f"agentx is open {'in an ' + how if how == 'app window' else 'in your browser' if how else 'at'}: {url}",
                  flush=True)
        print("Closing the window quits agentx; so does Ctrl+C here." if self.open_it else
              "Keep this terminal open; close it or press Ctrl+C here to quit agentx.", flush=True)
        try:
            while self._worker.is_alive():
                self._worker.join(0.5)
                if self.window_closed():
                    print("The window was closed, so agentx is quitting. Your conversation is saved; "
                          "agentx --ui --continue picks it up.", flush=True)
                    self.quit_for_closed_window()
                    self._worker.join(10)
                    break
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
        """Only a page on 127.0.0.1 or localhost (any port: an SSH tunnel may use another one at home), so another
        site's page that points a DNS name at this machine cannot use the window."""
        host = self.headers.get("Host", "").strip().lower()
        name, sep, port = host.rpartition(":")
        if not sep or not port.isdigit():
            name = host
        elif name in ("127.0.0.1", "localhost"):
            return True
        tailnet = (self.front.tailnet or "").lower()        # Tailscale serve passes the browser's name through
        return bool(tailnet) and name == tailnet

    def _who(self) -> str:
        """Where a window is: ``tailscale:<account>`` when Tailscale served it, else the address it came from."""
        login = (self.headers.get("Tailscale-User-Login") or "").strip()
        if login:
            return "tailscale:" + login[:80]
        return str(self.client_address[0]) if self.client_address else ""

    def _device(self, query: Dict[str, List[str]]) -> str:
        """The device a request comes from (its window sends the id it keeps); "" from an older page."""
        return _device_id(self.headers.get("X-Agentx-Device") or (query.get("d") or [""])[0])

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
        device = self._device(query)
        front = self.front.route(device)                    # this device's own conversation (or the main one)
        if url.path == "/events":
            after = self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0] or "0"
            try:
                return self._events(max(0, int(after)), front)
            except ValueError:
                return self._events(0, front)
        if url.path == "/api/state":
            return self._json({**front.state(), "live": self.front.live_info(device)})
        if url.path == "/api/sessions":
            return self._json(front.sessions())
        if url.path == "/api/history":
            raw = (query.get("upto") or [""])[0]
            return self._json(front.history_payload(int(raw) if raw.isdigit() else None))
        if url.path == "/api/details":
            return self._json(front.details())
        if url.path == "/api/settings":
            return self._json(front.settings_view())
        if url.path == "/api/files":
            return self._json(front.find_files((query.get("q") or [""])[0]))
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
        device = self._device(parse_qs(url.query))
        if url.path == "/api/join":                         # open the main conversation, or go back to its own
            return self._json(self.front.join(device, bool(data.get("live")), str(data.get("then") or "")))
        front = self.front.route(device)
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
        if url.path == "/api/settings":
            return self._json(front.save_settings(str(data.get("scope", "")), data.get("values")))
        if url.path == "/api/open-folder":
            return self._json(front.open_folder(str(data.get("path", ""))))
        if url.path == "/api/mcp-signin":
            return self._json(front.finish_signin(str(data.get("url", ""))))
        return self._json({"error": "not found"}, 404)

    def _events(self, after: int, front: Optional[WebFront] = None) -> None:
        """Server-sent events: everything after ``after``, then each new one as it comes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = after
        front = front or self.front
        front.window_connected(True, self._who())
        try:
            while True:
                events = front.bus.after(last, wait=KEEP_ALIVE_SEC)
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
        finally:
            front.window_connected(False, self._who())
