"""/remote-control in the terminal: the same session in a window; answers from the window go into the terminal."""

import http.client
import io
import json
import queue
import threading
import time

import pytest
from rich.console import Console

from agentx_dev.cli import remote as remote_module
from agentx_dev.cli.app import ChatApp
from tests.conftest import MockModel, make_final, make_react_response


class FakePrompt:
    """The terminal prompt, without a terminal: what is "typed" arrives through ``typed``; menu picks through ``picks``."""

    def __init__(self):
        self.mirror = None
        self._chat = object()
        self.typed = queue.Queue()
        self.picks = queue.Queue()

    def type_in(self, kind, text):
        self.typed.put(text)
        return True

    def read(self, prompt="> ", default=""):
        if self.mirror is not None:
            return self.mirror.around("message", lambda: self.typed.get(timeout=20), prompt=prompt)
        return self.typed.get(timeout=20)

    def read_short(self, prompt=""):
        if self.mirror is not None:
            return self.mirror.around("text", lambda: self.typed.get(timeout=20), prompt=prompt)
        return self.typed.get(timeout=20)

    def select(self, options, default=0, hotkeys=None):
        if self.mirror is not None:
            return self.mirror.around("select", lambda: self.picks.get(timeout=20), options=list(options),
                                      default=default)
        return self.picks.get(timeout=20)


def wait_for(check, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("never happened")


def request(front, method, path, body=None):
    port = front._server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}", "X-Agentx-Token": front.token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return json.loads(raw)


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("agentx_dev.cli.webui.tailscale_name", lambda: (None, ""))
    prompt = FakePrompt()
    monkeypatch.setattr(remote_module, "pick_in_menu", lambda index: prompt.picks.put(index) or True)
    cwd, home = tmp_path / "proj", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    script = [make_react_response("write_file", {"path": "hello.txt", "content": "hi\n", "if_exists": "refuse"}),
              make_final("Created hello.txt")]
    out = io.StringIO()
    app = ChatApp(cwd=cwd, home=home, console=Console(file=out, width=100), read_line=prompt.read,
                  read_short=prompt.read_short, select=prompt.select, env={},
                  model_factory=lambda name, env: MockModel(script=script))
    app.prompt_input = prompt
    thread = threading.Thread(target=app.start, daemon=True)
    thread.start()
    yield app, prompt, out, cwd
    prompt.typed.put("/exit")
    thread.join(10)
    remote = getattr(app, "_remote", None)
    if remote is not None:
        remote.stop()


def test_the_window_drives_the_terminal_session(terminal):
    app, prompt, out, cwd = terminal
    prompt.typed.put("/remote-control")                              # typed in the terminal
    remote = wait_for(lambda: getattr(app, "_remote", None))
    front = remote.front
    assert "ssh -N -L" in out.getvalue() and "remote-control off" in out.getvalue()
    # the window has the open message box, and sends a message: it is typed into the terminal's prompt
    wait_for(lambda: any(p["kind"] == "message" for p in front._pending.values()))
    assert request(front, "POST", "/api/send", {"text": "create hello.txt"})["result"] == "sent"
    # the approval is a card in the window and the terminal's menu at once; Yes in the window picks it there
    ask = wait_for(lambda: next((e for e in front.bus.after(0) if e["type"] == "ask" and e["kind"] == "approval"), None))
    assert "hello.txt" in ask["title"]
    assert request(front, "POST", "/api/answer", {"id": ask["id"], "value": {"choice": "y"}})["ok"] is True
    wait_for(lambda: (cwd / "hello.txt").exists())
    wait_for(lambda: any(e["type"] == "final" for e in front.bus.after(0)))
    kinds = [e["type"] for e in front.bus.after(0)]
    assert "user" in kinds and "tool_call" in kinds
    assert any(e["type"] == "user" and e["text"] == "create hello.txt" for e in front.bus.after(0))
    assert any(e["type"] == "answered" and e.get("outcome") == "Approved" for e in front.bus.after(0))
    assert "Created hello.txt" in out.getvalue()                     # the terminal showed it all too
    # and off again, from the window
    wait_for(lambda: any(p["kind"] == "message" for p in front._pending.values()))
    request(front, "POST", "/api/send", {"text": "/remote-control off"})
    wait_for(lambda: getattr(app, "_remote", "x") is None)
    assert prompt.mirror is None and type(app.screen).__name__ != "_TeeScreen"


def test_a_terminal_answer_closes_the_window_card(terminal):
    app, prompt, out, cwd = terminal
    prompt.typed.put("/remote-control")
    remote = wait_for(lambda: getattr(app, "_remote", None))
    front = remote.front
    wait_for(lambda: any(p["kind"] == "message" for p in front._pending.values()))
    prompt.typed.put("create hello.txt")                             # typed in the terminal this time
    ask = wait_for(lambda: next((e for e in front.bus.after(0) if e["type"] == "ask" and e["kind"] == "approval"), None))
    prompt.picks.put(2)                                              # "No" in the terminal's menu
    prompt.typed.put("not now")                                      # what it should do instead
    wait_for(lambda: any(e["type"] == "answered" and e["id"] == ask["id"] for e in front.bus.after(0)))
    assert not (cwd / "hello.txt").exists()
    assert any(e["type"] == "answered" and e["id"] == ask["id"] and e.get("outcome") == "Declined"
               for e in front.bus.after(0))


def test_remote_control_needs_the_terminal_prompt(tmp_path):
    out = io.StringIO()
    app = ChatApp(cwd=tmp_path, home=tmp_path, console=Console(file=out, width=100), read_line=lambda *a: "/exit",
                  env={}, model_factory=lambda name, env: MockModel(script=[make_final("ok")]))
    app.remote_control("")
    assert "needs this terminal's own prompt" in out.getvalue()
    app.remote_control("off")
    assert "not on" in out.getvalue()


class TestWhenTailscaleGivesNoLink:
    """It worked, then HTTPS / Serve was turned off in the Tailscale account: the terminal showed only the SSH way,
    with the reason buried under it."""

    def test_the_reason_comes_first(self):
        from agentx_dev.cli.webui import TAILSCALE_HTTPS_HELP, remote_instructions
        text = remote_instructions("http://127.0.0.1:4529/?t=k", ssh_running=True, addresses=["10.0.0.5"],
                                   tailnet_problem=TAILSCALE_HTTPS_HELP, firewall="")
        assert text.startswith("No link for your iPad or phone this time")
        assert "login.tailscale.com/admin/dns" in text and "ssh -N -L 4529" in text

    def test_serve_turned_off_passes_on_tailscales_own_link(self, monkeypatch):
        from agentx_dev.cli import webui
        said = ("Serve is not enabled on your tailnet.\nTo enable, visit:\n\n"
                "         https://login.tailscale.com/f/serve?node=abc123")
        monkeypatch.setattr(webui, "_tailscale", lambda args, timeout=20: (124, said))
        problem = webui.tailscale_serve(4529)
        assert problem.startswith("No link for your iPad") and "https://login.tailscale.com/f/serve?node=abc123" in problem

    def test_a_tailscale_that_waits_still_says_what_it_printed(self, monkeypatch):
        import subprocess
        from agentx_dev.cli import webui

        def waits(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=1, output="Serve is not enabled on your tailnet.")
        monkeypatch.setattr(webui, "_tailscale_exe", lambda: "tailscale")
        monkeypatch.setattr(webui.subprocess, "run", waits)
        assert webui._tailscale(["serve"]) == (124, "Serve is not enabled on your tailnet.")


def test_agentx_remote_opens_this_terminal_with_remote_control_on(monkeypatch):
    """agentx --remote (no --ui): the terminal, with /remote-control already on (and the normal screen, whose prompt
    it mirrors)."""
    from agentx_dev.cli import app as app_module
    args = app_module.build_parser().parse_args(["--remote"])
    assert args.remote and not args.ui
    source = open(app_module.__file__, encoding="utf-8").read()
    assert 'app.remote_at_start = bool(getattr(args, "remote", False)) and not getattr(args, "ui", False)' in source
    assert "if args.remote:\n        wanted = False" in source
    start = source.split("    def start(self", 1)[1].split("\n    def ", 1)[0]
    assert "if getattr(self, \"remote_at_start\", False):" in start and "self.remote_control()" in start


class TestWhatTheIpadSees:
    def test_the_terminal_spinner_counts_as_working_and_the_tokens_go_along(self, tmp_path):
        """/remote-control mirrors the normal screen, whose spinner is a rich Live (``_live``), not ``_shown``: the
        window said "Waiting for agentx" with nothing moving while the terminal worked."""
        from types import SimpleNamespace
        from agentx_dev.cli.webui import WebFront
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        progress = SimpleNamespace(_shown=False, _live=object(), _since=0.0, _label="Thinking")
        front.app = SimpleNamespace(progress=progress, _usage_totals=lambda: (3, 12000, 800, 0), _turn_start=2000,
                                    _waiting_shown=lambda: [], _team_rows={}, _team_steps=(0, 0),
                                    recording=lambda: False, gate=SimpleNamespace(mode="default"), model_label="m")
        status = front._status()
        assert status["working"] is True and status["tokens"] > 0
        progress._live = None
        assert front._status()["working"] is False

    def test_the_ways_in_stay_in_the_terminal(self):
        """Whoever opens the window already got there: the link and the ssh steps are for the terminal only."""
        shown, sent = [], []
        rc = remote_module.RemoteControl.__new__(remote_module.RemoteControl)
        rc.active = True
        rc._quiet = remote_module._Quiet()
        terminal = type("T", (), {"notice": lambda self, text: shown.append(text)})()
        web = type("W", (), {"notice": lambda self, text: sent.append(text)})()
        rc._saved = {"screen": terminal}
        rc.app = type("A", (), {"screen": remote_module._TeeScreen(terminal, web, rc._quiet)})()
        rc.tell_terminal("open https://pc.tail.ts.net:4529/?t=k")
        assert shown and not sent


def test_ssh_is_offered_on_the_wifi_address_first():
    """With Tailscale on, the ssh command used the 100.x address, which times out from a laptop without Tailscale."""
    from agentx_dev.cli.webui import remote_instructions
    text = remote_instructions("http://127.0.0.1:4529/?t=k", ssh_running=True, firewall="",
                               addresses=["100.113.0.53", "10.0.0.219"])
    assert "bruce@10.0.0.219" in text or "@10.0.0.219" in text
    assert "use 100.113.0.53 (its Tailscale address)" in text
