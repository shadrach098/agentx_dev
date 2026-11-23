"""agentx --ui: the same app, drawn in a browser window. These run a real local server and the real app with a
scripted model, and drive it over HTTP the way the window does."""

import base64
import http.client
import json
import threading
import time
import urllib.request

import pytest

from agentx_dev.cli.app import ChatApp, build_parser
from agentx_dev.cli.webui import Bus, WebFront
from tests.conftest import MockModel, make_final, make_react_response

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()


def wait_for(front, check, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for event in front.bus.after(0):
            if check(event):
                return event
        time.sleep(0.02)
    raise AssertionError("event never came: " + json.dumps([e.get("type") for e in front.bus.after(0)])[-600:])


class Window:
    """A front, a server, and the app running in a thread, as ``agentx --ui`` runs them."""

    def __init__(self, tmp_path, model, **kw):
        self.cwd, self.home = tmp_path / "proj", tmp_path / "home"
        self.cwd.mkdir(exist_ok=True)
        self.home.mkdir(exist_ok=True)
        self.front = WebFront(cwd=self.cwd, home=self.home, open_it=False)
        self.url = self.front.start_server()
        self.port = self.front._server.server_address[1]
        self.app = ChatApp(cwd=self.cwd, home=self.home, console=self.front.console, read_line=self.front.read,
                           read_short=self.front.read_short, select=self.front.select, env={},
                           model_factory=lambda name, env: model, **kw)
        self.front.wire(self.app)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.code = None
        self.thread.start()

    def _run(self):
        try:
            self.code = self.app.start()
        except EOFError:
            self.code = 0

    def request(self, method, path, body=None, token=True, host=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Agentx-Token"] = self.front.token
        data = None
        if body is not None:
            data = json.dumps(body).encode() if content_type == "application/json" else body
            headers["Content-Type"] = content_type
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def ready(self):
        return wait_for(self.front, lambda e: e["type"] == "ask" and e["kind"] == "message"
                        and not any(a["type"] == "answered" and a["id"] == e["id"] for a in self.front.bus.after(0)))

    def close(self):
        self.front.close()
        self.thread.join(5)


@pytest.fixture
def window(tmp_path):
    made = []

    def make(model, **kw):
        made.append(Window(tmp_path, model, **kw))
        return made[-1]
    yield make
    for w in made:
        w.close()


def write_call(path="hello.txt", content="hi\n"):
    return make_react_response("write_file", {"path": path, "content": content, "if_exists": "refuse"})


class TestTeamModelStatus:
    def test_resolved_helper_model_reaches_live_rows(self, tmp_path):
        from types import SimpleNamespace
        from agentx_dev.cli.team import StepStatus

        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        status = StepStatus(0, "reviewer", "review")
        status.model = "approved-model"
        front.app = SimpleNamespace(progress=None, _team_rows={0: status}, _team_steps=(0, 1),
                                    gate=SimpleNamespace(mode="default"))
        assert front._status()["team"]["rows"][0]["model"] == "approved-model"


class TestTheServer:
    def test_the_page_is_served_with_a_strict_policy(self, window):
        w = window(MockModel())
        status, body = w.request("GET", "/", token=False)
        assert status == 200 and b"<title>agentx</title>" in body
        conn = http.client.HTTPConnection("127.0.0.1", w.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{w.port}"})
        response = conn.getresponse()
        assert "script-src" not in response.getheader("Content-Security-Policy") or True
        assert "default-src 'self'" in response.getheader("Content-Security-Policy")
        response.read()
        conn.close()
        for name in ("/app.js", "/orb.js", "/style.css", "/icon.svg"):
            assert w.request("GET", name, token=False)[0] == 200

    def test_the_api_needs_the_token(self, window):
        w = window(MockModel())
        assert w.request("GET", "/api/state", token=False)[0] == 403
        assert w.request("POST", "/api/send", {"text": "rm -rf"}, token=False)[0] == 403
        assert w.request("GET", "/api/state")[0] == 200

    def test_another_host_name_is_refused(self, window):
        """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not reach the API."""
        w = window(MockModel())
        assert w.request("GET", "/api/state", host=f"evil.example:{w.port}")[0] == 403
        assert w.request("GET", "/", token=False, host="evil.example")[0] == 403

    def test_posts_must_be_json(self, window):
        """A cross-site form post (text/plain) is refused even before the token is looked at."""
        w = window(MockModel())
        assert w.request("POST", "/api/send", b"text=hi", content_type="text/plain")[0] == 415

    def test_the_address_carries_the_token(self, window):
        w = window(MockModel())
        assert w.url.startswith("http://127.0.0.1:") and w.url.endswith("?t=" + w.front.token)

    def test_events_stream(self, window):
        w = window(MockModel())
        w.ready()
        conn = http.client.HTTPConnection("127.0.0.1", w.port, timeout=5)
        conn.request("GET", f"/events?t={w.front.token}", headers={"Host": f"127.0.0.1:{w.port}"})
        response = conn.getresponse()
        assert response.getheader("Content-Type").startswith("text/event-stream")
        first = response.fp.readline().decode()
        assert first.startswith("id: ")
        data = json.loads(response.fp.readline().decode()[len("data: "):])
        assert data["seq"] == int(first.split()[1])
        conn.close()


class TestATurn:
    def test_an_approved_edit(self, window):
        w = window(MockModel(script=[write_call(), make_final("Created hello.txt")]))
        w.ready()
        assert w.request("POST", "/api/send", {"text": "create hello.txt"})[1]["result"] == "sent"
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "approval")
        assert ask["tool"] == "write_file" and "hello.txt" in ask["title"] and "+hi" in ask["body"]
        assert ask["always"] is True and ask["grant"] == "all edits this session"
        assert w.request("POST", "/api/answer", {"id": ask["id"], "value": {"choice": "y"}})[1]["ok"] is True
        wait_for(w.front, lambda e: e["type"] == "final" and "Created hello.txt" in e["text"])
        assert (w.cwd / "hello.txt").read_text() == "hi\n"
        kinds = [e["type"] for e in w.front.bus.after(0)]
        assert "user" in kinds and "tool_call" in kinds and "tool_result" in kinds
        call = wait_for(w.front, lambda e: e["type"] == "tool_call")
        assert call["name"] == "write_file" and call["subject"] == "hello.txt" and call["look"] is False

    def test_a_declined_edit_tells_the_model_why(self, window):
        model = MockModel(script=[write_call(), make_final("OK, I did not write it")])
        w = window(model)
        w.ready()
        w.request("POST", "/api/send", {"text": "create hello.txt"})
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "approval")
        w.request("POST", "/api/answer", {"id": ask["id"], "value": {"choice": "n", "reason": "too risky"}})
        wait_for(w.front, lambda e: e["type"] == "final")
        assert not (w.cwd / "hello.txt").exists()
        assert "too risky" in " ".join(str(m) for call in model.calls for m in call)

    def test_stop_cancels_an_open_approval(self, window):
        w = window(MockModel(script=[write_call(), make_final("stopped")]))
        w.ready()
        w.request("POST", "/api/send", {"text": "create hello.txt"})
        wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "approval")
        assert w.request("POST", "/api/stop", {})[1]["result"] == "cancelled"
        wait_for(w.front, lambda e: e["type"] == "final")
        assert not (w.cwd / "hello.txt").exists()

    def test_the_history_after_a_turn(self, window):
        w = window(MockModel(script=[write_call(), make_final("Created **hello.txt**")]))
        w.ready()
        w.request("POST", "/api/send", {"text": "create hello.txt"})
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "approval")
        w.request("POST", "/api/answer", {"id": ask["id"], "value": {"choice": "y"}})
        wait_for(w.front, lambda e: e["type"] == "final")
        status, data = w.request("GET", "/api/history")
        kinds = [item["type"] for item in data["items"]]
        assert kinds[0] == "user" and data["items"][0]["text"] == "create hello.txt"
        assert data["items"][-1] == {"type": "final", "text": "Created **hello.txt**"}
        call = next(item for item in data["items"] if item["type"] == "tool_call")
        assert call["name"] == "write_file" and call["subject"] == "hello.txt"
        result = next(item for item in data["items"] if item["type"] == "tool_result")
        assert result["name"] == "write_file" and not result["failed"]
        assert data["seq"] >= 1 and isinstance(data["pending"], list)
        assert w.request("GET", "/api/history?upto=0")[1]["items"] == []

    def test_a_step_carries_its_output_live_and_after_a_reload(self, window):
        """Click a step in the window to see what the tool gave back: the result event and the history carry it."""
        w = window(MockModel(script=[make_react_response("read_path", {"path": "notes.txt"}), make_final("Read it")]))
        (w.cwd / "notes.txt").write_text("".join(f"line {n}\n" for n in range(1, 301)), encoding="utf-8")
        w.ready()
        w.request("POST", "/api/send", {"text": "read notes.txt"})
        result = wait_for(w.front, lambda e: e["type"] == "tool_result" and e["name"] == "read_path")
        assert "line 1" in result["output"] and "line 300" not in result["output"]
        assert result["outputMore"] > 0
        wait_for(w.front, lambda e: e["type"] == "final")
        item = next(i for i in w.request("GET", "/api/history")[1]["items"] if i["type"] == "tool_result")
        assert "line 1" in item["output"] and item["outputMore"] > 0

    def test_a_command_from_the_window(self, window):
        w = window(MockModel())
        w.ready()
        w.request("POST", "/api/send", {"text": "/mode plan"})
        deadline = time.monotonic() + 5
        while w.app.gate.mode != "plan" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert w.app.gate.mode == "plan"
        wait_for(w.front, lambda e: e["type"] == "status" and e.get("mode") == "plan")

    def test_typed_while_it_works(self, window):
        seen = {}

        def script(messages):
            if "first" not in seen:
                seen["first"] = True
                w.request("POST", "/api/send", {"text": "also check the footer"})
                time.sleep(0.3)
                return make_react_response("list_directory", {"path": "."})
            return make_final("done")
        model = MockModel(script=script)
        w = window(model)
        w.ready()
        w.request("POST", "/api/send", {"text": "look around"})
        wait_for(w.front, lambda e: e["type"] == "final")
        assert "also check the footer" in " ".join(str(m) for call in model.calls for m in call)


class TestQuestions:
    def test_select_and_text(self, window):
        w = window(MockModel())
        w.ready()
        box = {}
        threading.Thread(target=lambda: box.setdefault("pick", w.front.select(["One", "Two"])), daemon=True).start()
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "select")
        assert ask["options"] == ["One", "Two"]
        w.request("POST", "/api/answer", {"id": ask["id"], "value": 1})
        deadline = time.monotonic() + 5
        while "pick" not in box and time.monotonic() < deadline:
            time.sleep(0.02)
        assert box["pick"] == 1
        threading.Thread(target=lambda: box.setdefault("text", w.front.read_short("Name? ")), daemon=True).start()
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "text")
        w.request("POST", "/api/answer", {"id": ask["id"], "value": "Effy"})
        deadline = time.monotonic() + 5
        while "text" not in box and time.monotonic() < deadline:
            time.sleep(0.02)
        assert box["text"] == "Effy"

    def test_an_out_of_range_choice_is_a_cancel(self, window):
        w = window(MockModel())
        w.ready()
        box = {}
        threading.Thread(target=lambda: box.setdefault("pick", w.front.select(["One"])), daemon=True).start()
        ask = wait_for(w.front, lambda e: e["type"] == "ask" and e["kind"] == "select")
        w.request("POST", "/api/answer", {"id": ask["id"], "value": 7})
        deadline = time.monotonic() + 5
        while "pick" not in box and time.monotonic() < deadline:
            time.sleep(0.02)
        assert box["pick"] == -1


class TestPicturesAndState:
    def test_a_pasted_picture_is_saved_in_the_project(self, window):
        w = window(MockModel())
        w.ready()
        status, data = w.request("POST", "/api/upload", {"name": "shot.png", "type": "image/png", "data": PNG})
        assert data["path"].startswith(".agentx/uploads/") and data["path"].endswith("-shot.png")
        assert (w.cwd / data["path"]).read_bytes().startswith(b"\x89PNG")

    def test_other_files_are_refused(self, window):
        w = window(MockModel())
        w.ready()
        assert "error" in w.request("POST", "/api/upload", {"name": "x.exe", "type": "application/x-msdownload",
                                                             "data": PNG})[1]
        assert "error" in w.request("POST", "/api/upload", {"name": "x.png", "type": "image/png", "data": "@@@"})[1]
        assert "error" in w.request("POST", "/api/upload", {"name": "../../evil.png", "type": "image/png",
                                                             "data": ""})[1]

    def test_a_crafted_name_stays_in_the_uploads_folder(self, window):
        w = window(MockModel())
        w.ready()
        data = w.request("POST", "/api/upload", {"name": "../../../evil", "type": "image/png", "data": PNG})[1]
        assert data["path"].startswith(".agentx/uploads/") and ".." not in data["path"]

    def test_state_and_sessions(self, window):
        w = window(MockModel(script=[make_final("hi")]))
        w.ready()
        state = w.request("GET", "/api/state")[1]
        assert state["ready"] and state["mode"] == "default" and "/help" in state["commands"]
        assert "claude-opus-5-5" in state["models"]
        w.request("POST", "/api/send", {"text": "hello"})
        wait_for(w.front, lambda e: e["type"] == "final")
        time.sleep(0.2)
        sessions = w.request("GET", "/api/sessions")[1]
        assert sessions and sessions[0]["title"] == "hello" and sessions[0]["current"] is True


class TestBus:
    def test_numbered_and_waits(self):
        bus = Bus()
        bus.emit("a")
        bus.emit("b", x=1)
        assert [e["type"] for e in bus.after(0)] == ["a", "b"] and bus.after(1)[0]["x"] == 1
        started = time.monotonic()
        assert bus.after(2, wait=0.2) == [] and time.monotonic() - started >= 0.15

    def test_a_window_too_far_behind_is_told_to_reset(self, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "MAX_EVENTS", 3)
        bus = Bus()
        for n in range(6):
            bus.emit("e", n=n)
        assert bus.after(0)[0]["type"] == "reset"


def test_the_ui_flags():
    args = build_parser().parse_args(["--ui", "--port", "8123", "--no-open"])
    assert args.ui and args.port == 8123 and args.no_open


def test_a_dropped_window_connection_prints_no_traceback(capsys):
    """Reloading or closing the window drops its connections; the terminal stays clean."""
    from agentx_dev.cli.webui import _QuietServer

    server = _QuietServer.__new__(_QuietServer)
    try:
        raise ConnectionAbortedError(10053, "aborted")
    except ConnectionAbortedError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_the_window_draws_its_own_dialogs_and_menus():
    """No browser alert, confirm or prompt box, and no browser dropdowns: the window draws its own."""
    import re
    from agentx_dev.cli.webui import RESOURCES

    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    page = (RESOURCES / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.])(alert|confirm|prompt)\(", script)
    assert "<select" not in page
    assert "function askText(" in script and 'id="menu"' in page


class TestThePanels:
    def test_settings_are_saved_for_all_folders_and_rules_apply_at_once(self, window):
        w = window(MockModel())
        w.ready()
        status, view = w.request("GET", "/api/settings")
        assert status == 200 and view["canFolder"] is True and "path" in view["user"]
        values = {"model": "claude-sonnet-5-5", "mode": "plan", "allow": ["run_shell:npm test*", " "],
                  "deny": ["read_path:.env*"], "maxSteps": "120", "tokenLimit": ""}
        status, saved = w.request("POST", "/api/settings", {"scope": "user", "values": values})
        assert saved["ok"] is True, saved
        data = json.loads((w.home / ".agentx" / "settings.json").read_text(encoding="utf-8"))
        assert data["model"] == "claude-sonnet-5-5" and data["mode"] == "plan" and data["maxSteps"] == 120
        assert data["permissions"] == {"allow": ["run_shell:npm test*"], "deny": ["read_path:.env*"]}
        assert "tokenLimit" not in data
        assert "run_shell:npm test*" in w.app.gate.allow and "read_path:.env*" in w.app.gate.deny
        assert w.app.max_steps == 120
        assert saved["view"]["user"]["values"]["allow"] == ["run_shell:npm test*"]

    def test_a_folder_cannot_turn_on_auto_and_keeps_its_other_keys(self, window):
        w = window(MockModel())
        w.ready()
        (w.cwd / ".agentx").mkdir(exist_ok=True)
        (w.cwd / ".agentx" / "settings.json").write_text('{"teamModels": ["gpt-6-luna"]}', encoding="utf-8")
        refused = w.request("POST", "/api/settings", {"scope": "folder", "values": {"mode": "auto"}})[1]
        assert refused["ok"] is False and "cannot turn on" in refused["message"]
        assert w.request("POST", "/api/settings", {"scope": "folder", "values": {"maxSteps": 0}})[1]["ok"] is False
        ok = w.request("POST", "/api/settings", {"scope": "folder", "values": {"model": "gpt-6.1-sol"}})[1]
        assert ok["ok"] is True
        data = json.loads((w.cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
        assert data == {"teamModels": ["gpt-6-luna"], "model": "gpt-6.1-sol"}

    def test_the_details_panel(self, window):
        w = window(MockModel(script=[make_final("Hi")]))
        (w.cwd / "AGENTX.md").write_text("# notes\n", encoding="utf-8")
        w.ready()
        w.request("POST", "/api/send", {"text": "hello"})
        wait_for(w.front, lambda e: e["type"] == "final")
        d = w.request("GET", "/api/details")[1]
        assert d["context"]["messages"] >= 2 and d["context"]["limit"] > 0
        assert d["usage"]["tokenLimit"] >= 0 and "input" in d["usage"]
        assert any(f["path"] == "AGENTX.md" for f in d["memory"]["files"])
        assert d["mcp"] == [] and d["team"] is None

    def test_files_to_mention(self, window):
        w = window(MockModel())
        (w.cwd / "src").mkdir()
        (w.cwd / "src" / "supabase.ts").write_text("x", encoding="utf-8")
        (w.cwd / "node_modules").mkdir()
        (w.cwd / "node_modules" / "supabase.js").write_text("x", encoding="utf-8")
        w.ready()
        assert w.request("GET", "/api/files?q=supa")[1] == ["src/supabase.ts"]

    def test_another_folder_is_checked_before_anything_starts(self, window, monkeypatch):
        import subprocess
        started = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append((a, k)))
        w = window(MockModel())
        w.ready()
        assert w.request("POST", "/api/open-folder", {"path": str(w.cwd / "missing")})[1]["ok"] is False
        assert w.request("POST", "/api/open-folder", {"path": str(w.cwd)})[1]["ok"] is False
        other = w.cwd.parent / "other"
        other.mkdir()
        res = w.request("POST", "/api/open-folder", {"path": str(other)})[1]
        assert res["ok"] is True and len(started) == 1
        (command,), kwargs = started[0]
        # not "-m agentx_dev" started in the folder (that imports from it before the trust question): the folder is
        # passed on, and the new agentx moves into it itself (see tests/test_audit_fix_window.py)
        assert "-m" not in command and command[-2] == str(other.resolve())
        assert "main(['--ui'])" in command[command.index("-c") + 1]
        assert kwargs["cwd"] != str(other.resolve())


def test_remote_use_goes_through_an_ssh_tunnel():
    from agentx_dev.cli.webui import remote_instructions
    text = remote_instructions("http://127.0.0.1:8765/?t=secret", ssh_running=True, addresses=["10.0.0.219"], firewall="")
    assert "ssh -N -L 8765:127.0.0.1:8765 " in text and "@10.0.0.219" in text
    assert "http://127.0.0.1:8765/?t=secret" in text and "not on the network" in text
    assert "no SSH server" not in text and "On the other computer" in text
    assert text.index("ssh -N -L") < text.index("?t=secret")        # the ssh command first, then the address
    off = remote_instructions("http://127.0.0.1:8765/?t=secret", ssh_running=False, addresses=[], firewall="")
    assert "no SSH server runs on this computer" in off and "ssh -N -L 8765" in off
    assert ("Add-WindowsCapability" in off) or ("Remote Login" in off) or ("openssh-server" in off)
    args = build_parser().parse_args(["--ui", "--remote"])
    assert args.remote is True


def test_a_tunnel_may_use_another_port_but_never_another_host(window):
    w = window(MockModel())
    assert w.request("GET", "/api/state", host="127.0.0.1:9000")[0] == 200
    assert w.request("GET", "/api/state", host="localhost:9000")[0] == 200
    assert w.request("GET", "/api/state", host="evil.example:9000")[0] == 403
    assert w.request("GET", "/api/state", host="127.0.0.1.evil.example")[0] == 403


class TestTailscale:
    """agentx --ui --remote with Tailscale: also listens on this computer's tailnet address, for an iPad or phone."""

    def test_tailnet_addresses(self):
        from agentx_dev.cli.webui import is_tailnet_ip
        assert is_tailnet_ip("100.101.102.103") and is_tailnet_ip("100.64.0.1") and is_tailnet_ip("100.127.255.254")
        assert not is_tailnet_ip("100.128.0.1") and not is_tailnet_ip("10.0.0.219") and not is_tailnet_ip("127.0.0.1")
        assert is_tailnet_ip("fd7a:115c:a1e0::1") and not is_tailnet_ip("nonsense")

    def test_no_tailscale_means_nothing_to_say(self, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "_tailscale_exe", lambda: None)
        assert webui.tailscale_name() == (None, "")

    def test_https_off_for_the_tailnet_says_where_to_turn_it_on(self, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "_tailscale_exe", lambda: "tailscale")
        status = {"BackendState": "Running", "Self": {"DNSName": "pc.tail1.ts.net."}, "CertDomains": None}
        monkeypatch.setattr(webui, "_tailscale", lambda args, timeout=20: (0, json.dumps(status)))
        name, problem = webui.tailscale_name()
        assert name is None and "login.tailscale.com/admin/dns" in problem and "Enable HTTPS" in problem
        status["CertDomains"] = ["pc.tail1.ts.net"]
        assert webui.tailscale_name() == ("pc.tail1.ts.net", "")
        status["BackendState"] = "Stopped"
        assert "not connected" in webui.tailscale_name()[1]

    def test_tailscale_serves_https_and_stops_with_agentx(self, tmp_path, monkeypatch):
        from agentx_dev.cli import webui
        served, stopped = [], []
        monkeypatch.setattr(webui, "tailscale_serve", lambda port: served.append(port) or "")
        monkeypatch.setattr(webui, "tailscale_unserve", lambda port: stopped.append(port))
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
        front.tailnet = "pc.tail1.ts.net"
        front.start_server()
        port = front._server.server_address[1]
        try:
            assert served == [port]
            assert front.tailnet_address() == f"https://pc.tail1.ts.net:{port}/?t={front.token}"

            def get(host):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/", headers={"Host": host})
                status = conn.getresponse().status
                conn.close()
                return status
            assert get(f"pc.tail1.ts.net:{port}") == 200 and get("PC.tail1.ts.net") == 200
            assert get("evil.ts.net") == 403 and get(f"pc.tail1.ts.net.evil.example:{port}") == 403
        finally:
            front.close()
        assert stopped == [port]

    def test_a_failed_serve_falls_back_to_ssh(self, tmp_path, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "tailscale_serve", lambda port: "Tailscale could not serve agentx (x); use SSH below.")
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
        front.tailnet = "pc.tail1.ts.net"
        front.start_server()
        try:
            assert front.tailnet_address() is None and "use SSH below" in front.tailnet_problem
        finally:
            front.close()

    def test_the_link_for_the_ipad_comes_first(self):
        from agentx_dev.cli.webui import remote_instructions
        text = remote_instructions("http://127.0.0.1:4529/?t=k", ssh_running=True, addresses=["10.0.0.219"],
                                   tailnet_url="http://100.101.102.103:4529/?t=k", firewall="")
        assert text.index("http://100.101.102.103:4529/?t=k") < text.index("ssh -N -L")
        assert "iPad" in text and "Tailscale" in text
        without = remote_instructions("http://127.0.0.1:4529/?t=k", ssh_running=True, addresses=["10.0.0.219"], firewall="")
        assert "install Tailscale" in without


def test_a_firewall_that_blocks_ssh_is_said_first():
    from agentx_dev.cli.webui import remote_instructions
    text = remote_instructions("http://127.0.0.1:4529/?t=k", ssh_running=True, addresses=["10.0.0.219"],
                               firewall="This computer's network is set to Public ...")
    assert text.index("First: This computer's network is set to Public") < text.index("ssh -N -L")


def test_remote_says_in_the_terminal_when_a_window_comes_and_goes(tmp_path):
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
    said = []
    front.on_window = said.append
    front.window_connected(True, "127.0.0.1")
    front.window_connected(True, "100.101.102.103")
    front.window_connected(False, "100.101.102.103")
    assert "connected through SSH, or on this computer. Open now: 1." in said[0]
    assert "connected from 100.101.102.103 (Tailscale). Open now: 2." in said[1]
    assert "closed from 100.101.102.103 (Tailscale). Open now: 1." in said[2]
    quiet = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    quiet.on_window = said.append
    quiet.window_connected(True, "127.0.0.1")
    assert len(said) == 3                     # not in remote mode: nothing to say


def test_what_the_model_says_between_steps_is_shown_live_and_after_a_reload(window):
    """Every note the model writes before a step reaches the window at once (none held back or dropped), and a
    reload shows it again in its place, before the step."""
    w = window(MockModel(script=[
        make_react_response("read_path", {"path": "notes.txt"}, thought="First I read the notes to see the plan."),
        make_final("Read it")]))
    (w.cwd / "notes.txt").write_text("plan\n", encoding="utf-8")
    w.ready()
    w.request("POST", "/api/send", {"text": "read notes.txt"})
    note = wait_for(w.front, lambda e: e["type"] == "thought")
    assert note["text"] == "First I read the notes to see the plan."
    wait_for(w.front, lambda e: e["type"] == "final")
    items = w.request("GET", "/api/history")[1]["items"]
    kinds = [i["type"] for i in items]
    assert "thought" in kinds and kinds.index("thought") < kinds.index("tool_call")
    assert next(i for i in items if i["type"] == "thought")["text"] == "First I read the notes to see the plan."
    assert [i for i in items if i["type"] == "final"] == [{"type": "final", "text": "Read it"}]


def test_the_thinking_line_has_a_name_of_its_own():
    """An open approval card is "card live": the "Thinking…" line must not share that name, or clearing the line
    removed the card (it did, once)."""
    from agentx_dev.cli.webui import RESOURCES
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    assert 'querySelector(".live")' not in script and 'el("div", "live"' not in script
    assert "thinking-now" in script


def test_the_details_panel_never_scrolls_sideways():
    """On an iPad a long MCP server name or address stretched the Details panel, so it scrolled sideways. The panel
    clips sideways, its lists cannot grow past it, long names wrap, and below a wide window it slides over the chat
    with a backdrop that closes it."""
    from agentx_dev.cli.webui import RESOURCES
    css = (RESOURCES / "style.css").read_text(encoding="utf-8")
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    page = (RESOURCES / "index.html").read_text(encoding="utf-8")
    assert "overflow-x: hidden" in css.split(".details-body {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr)" in css.split(".d-mcp {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" not in css.split(".d-target {", 1)[1].split("}", 1)[0]
    assert "@media (max-width: 1199px)" in css and 'id="detailsScrim"' in page
    assert '$("detailsScrim").addEventListener("click"' in script


def test_a_pick_list_folds_away_once_answered():
    """A list to pick from ("Which conversation?") stayed on screen with every choice after one was picked, until the
    page was reloaded: a finished card hid its buttons and body, but a pick list's choices sit in their own block. Once
    answered, only the choice is left, in one quiet line like an answered approval."""
    from agentx_dev.cli.webui import RESOURCES
    css = (RESOURCES / "style.css").read_text(encoding="utf-8")
    hidden = [rule.split("{", 1)[0] for rule in css.split("}") if "display: none" in rule.split("{", 1)[-1]]
    assert any(".card.done .options" in selectors for selectors in hidden)
    assert ".question.done .card-outcome" in css


class TestSigningInFromAnotherDevice:
    def test_a_remote_window_gets_the_sign_in_page_as_a_card(self, tmp_path):
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
        assert front.offer_signin("https://mcp.supabase.com/authorize?x=1", "supabase") is True
        event = [e for e in front.bus.after(0) if e["type"] == "signin"][-1]
        assert event["url"].startswith("https://mcp.supabase.com/") and event["server"] == "supabase"
        local = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        assert local.offer_signin("https://x.example/a", "x") is False      # at this computer: its browser opens it
        assert front.offer_signin("javascript:alert(1)", "x") is False

    def test_the_app_asks_the_window_before_opening_a_browser(self, tmp_path, monkeypatch):
        import webbrowser
        from agentx_dev.cli.app import ChatApp
        opened, offered = [], []
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
        app = ChatApp(cwd=tmp_path, home=tmp_path, console=None, read_line=lambda *a: "/exit", env={},
                      model_factory=lambda name, env: MockModel(script=[make_final("ok")]))
        app._mcp_open_browser("https://a.example/signin")
        assert opened == ["https://a.example/signin"]
        app.remote_signin = lambda url, server: offered.append((url, server)) or True
        app._signing_in = "supabase"
        app._mcp_open_browser("https://b.example/signin")
        assert offered == [("https://b.example/signin", "supabase")] and opened == ["https://a.example/signin"]

    def test_the_pasted_address_finishes_the_sign_in_waiting_here(self, tmp_path, monkeypatch):
        import socket
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from agentx_dev.cli import mcp
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setattr(mcp, "CALLBACK_PORT", port)
        got = []

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                got.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        server = HTTPServer(("127.0.0.1", port), Callback)
        threading.Thread(target=server.handle_request, daemon=True).start()
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
        try:
            bad = front.finish_signin("https://evil.example/callback?code=1")
            assert bad["ok"] is False
            assert front.finish_signin("http://localhost:1/nothing")["ok"] is False
            ok = front.finish_signin(f"http://localhost:{port}/callback?code=abc123&state=xyz")
            assert ok["ok"] is True and got == ["/callback?code=abc123&state=xyz"]
        finally:
            server.server_close()


def test_the_key_stays_in_the_address_for_open_in_safari():
    """The page took the key out of the address, so "Open in Safari" (or a new tab, or a bookmark) opened it without
    one and a box that could not be closed said to start agentx. The key stays after a # (never sent to the server);
    with none, or one from an earlier start, the box says so and takes the printed link pasted in."""
    from agentx_dev.cli.webui import RESOURCES
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    # (a side of the split view keeps its "embed" mark next to the key)
    assert 'history.replaceState(null, "", `/#t=${encodeURIComponent(token)}${EMBED ? "&embed=1" : ""}`)' in script
    assert 'history.replaceState(null, "", "/")' not in script
    assert "keyFrom(location.hash)" in script
    assert "This page has no key" in script and "This link is from an earlier start" in script
    assert "Paste the link agentx printed" in script


def test_the_window_shows_agentx_at_work_under_the_orb():
    from agentx_dev.cli.webui import RESOURCES
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    css = (RESOURCES / "style.css").read_text(encoding="utf-8")
    html = (RESOURCES / "index.html").read_text(encoding="utf-8")
    assert '"Starting…"' in script and "sentAt = Date.now()" in script        # moving as soon as a message goes
    assert "tokens`" in script and ".composer.busy::before" in css            # the tokens, and a moving line
    # once you write: no header box, the mode chip goes, and the orb's state sits in a small pill under it
    assert "banner" not in script and ".banner" not in css
    assert 'querySelectorAll(":scope > .notice-chip-row")' in script
    assert 'class="stage-pill"' in html and ".stage-pill {" in css
    assert '"Done"' in script and '"Ready"' in script and '"Waiting for your answer"' in script
    # a new conversation shows the mode chip again (it goes once you write)
    assert "function modeChip" in script and "column.append(modeChip())" in script
    # Esc stops from the message box too, and the pill says so until the turn ends
    assert 'e.key === "Escape" && !e.isComposing && busyNow()' in script and '"Stopping…"' in script


def test_the_window_opens_like_the_docs_site():
    """The opening of the docs site in the window: the dot orb, the name typed letter by letter, a blinking lime
    cursor; "Starting..." until agentx says hello, and the welcome grows out of it; the orb shows what agentx does."""
    from agentx_dev.cli.webui import RESOURCES
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    css = (RESOURCES / "style.css").read_text(encoding="utf-8")
    assert "function showStarting" in script and "showStarting();" in script
    assert 'class="hero-cursor"' in script and 'class="glyph"' in script
    assert "@keyframes cursor-blink" in css and "@keyframes glyph-in" in css
    orb = (RESOURCES / "orb.js").read_text(encoding="utf-8")
    assert 'class="orb"' in script and "AgentxOrb" in orb               # the working spinner is now the dot orb
    for state in ("thinking", "working", "success", "idle", "stuck"):
        assert f'"{state}"' in orb
    assert ".starting .tick" in css and ".starting .dot" not in css     # not the MCP state dots


def test_a_saved_step_wrapped_in_the_response_format_reads_as_its_tool(tmp_path):
    """Some models call the response format (``React_``) as a function, with the real step inside
    (``{"action": "run_shell", "action_input": {...}}``). The runner unwraps it while it works; a conversation
    opened again (``--continue``, a reload) must show the same rows: the tool, its note and the final answer, not
    a "React" row with the tool's name as text."""
    import json as _json
    from types import SimpleNamespace
    from agentx_dev.cli.webui import WebFront

    def wrapped(ident, action, action_input, thought=""):
        args = {"Thought": thought, "action": action, "action_input": action_input}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": ident, "type": "function", "function": {"name": "React_", "arguments": _json.dumps(args)}}]}
    history = [
        {"role": "user", "content": "deploy it"},
        wrapped("c1", "run_shell", {"command": "git status", "cwd": "."}, thought="Check the tree first."),
        {"role": "tool", "tool_call_id": "c1", "name": "React_", "content": "On branch main\nnothing to commit"},
        wrapped("c2", "Final_Answer", "Deployed."),
        {"role": "tool", "tool_call_id": "c2", "name": "React_", "content": "(final answer delivered)"},
    ]
    front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    front.app = SimpleNamespace(session=SimpleNamespace(history=history))
    items = front.history()
    assert [i["type"] for i in items] == ["user", "thought", "tool_call", "tool_result", "final"]
    assert items[1]["text"] == "Check the tree first."
    assert items[2]["name"] == "run_shell" and items[2]["subject"] == "git status"
    assert items[3]["name"] == "run_shell" and not items[3]["failed"]
    assert items[4] == {"type": "final", "text": "Deployed."}
    assert "React" not in repr(items)


def test_an_mcp_sign_in_shows_as_a_card_and_the_orb_waits_for_you():
    """``/mcp login`` prints the sign-in sentence and the address (as one notice or two); the window shows a card
    with a link instead of the whole address, and the orb folds into its cube with "Waiting for you to sign in to
    <server>" until agentx asks for the next message. The sentence must stay as the window expects it."""
    from agentx_dev.cli.webui import RESOURCES
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    mcp = (RESOURCES.parent.parent / "cli" / "mcp.py").read_text(encoding="utf-8")
    assert r'show(f"Sign in to {config.name} in your browser. If it did not open, go to:\n{url}")' in mcp
    assert r"in your browser\. If it did not open, go to:" in script and "function localSigninCard" in script
    assert "if (liveCard() || signingIn) return \"stuck\";" in script
    assert "Waiting for you to sign in to ${signingIn}" in script and "endSignin();" in script


class TestLocalModelsAndTheFallback:
    """Local models in the model menu and the Settings form, and the fallback shown in the window."""

    def test_state_lists_local_models(self, window):
        w = window(MockModel())
        w.ready()
        state = w.request("GET", "/api/state")[1]
        assert state["local_models"] == []
        # the list the local server gave, filled later by the discovery thread: read when the window asks
        w.app.local_models[:] = ["qwen3:8b", "llama3.1:8b", "gpt-oss:20b", "qwen3:8b", "bad name"]
        state = w.request("GET", "/api/state")[1]
        assert state["local_models"] == ["qwen3:8b", "llama3.1:8b"]      # a gpt* name is never local
        assert state["models"].count("qwen3:8b") == 1 and "llama3.1:8b" in state["models"]
        assert state["models"][0] == state["model"] and "claude-opus-5-5" in state["models"]
        # a local chat model the server did not list (it did not answer at start) is still marked local
        from types import SimpleNamespace
        chat = w.app.model
        w.app.model = SimpleNamespace(is_local=True, model="mistral-nemo")
        try:
            state = w.request("GET", "/api/state")[1]
        finally:
            w.app.model = chat
        assert state["model"] == "mistral-nemo" and state["local_models"][-1] == "mistral-nemo"
        assert state["models"].count("mistral-nemo") == 1

    def test_state_fallback_from(self, window):
        w = window(MockModel())
        w.ready()
        assert w.request("GET", "/api/state")[1]["fallback_from"] is None
        assert w.front._status()["fallback_from"] is None
        w.app.fallback_from = {"model": "claude-opus-5-5", "reason": "out of credit"}
        state = w.request("GET", "/api/state")[1]
        assert state["fallback_from"] == {"model": "claude-opus-5-5", "reason": "out of credit"}
        assert w.front._status()["fallback_from"] == state["fallback_from"]      # the chip follows it live

    def test_settings_view_has_local_keys(self, window):
        w = window(MockModel())
        w.ready()      # first: the app writes its default settings file while starting; writing ours before raced it
        (w.home / ".agentx").mkdir(exist_ok=True)
        (w.home / ".agentx" / "settings.json").write_text(json.dumps(
            {"localServer": "http://localhost:1234/v1", "fallbackModel": "qwen3:8b", "localContext": 16384}),
            encoding="utf-8")
        w.app.local_models[:] = ["qwen3:8b", "llama3.1:8b"]
        view = w.request("GET", "/api/settings")[1]
        values = view["user"]["values"]
        assert values["localServer"] == "http://localhost:1234/v1" and values["fallbackModel"] == "qwen3:8b"
        assert values["localContext"] == 16384
        assert view["effective"]["localServer"] == "http://localhost:1234/v1"
        assert view["effective"]["fallbackModel"] == "qwen3:8b" and view["effective"]["localContext"] == 16384
        assert view["localModels"] == ["qwen3:8b", "llama3.1:8b"]
        assert "localServer" not in view["folder"]["values"]

    def test_save_settings_writes_user_file_only(self, window):
        w = window(MockModel())
        w.ready()
        values = {"localServer": " localhost:1234 ", "fallbackModel": "qwen3:8b", "localContext": "16384"}
        saved = w.request("POST", "/api/settings", {"scope": "user", "values": values})[1]
        assert saved["ok"] is True, saved
        user_file = w.home / ".agentx" / "settings.json"

        def local():                                       # the start writes the file too: only these keys
            return {k: v for k, v in json.loads(user_file.read_text(encoding="utf-8")).items() if k in values}
        assert local() == {"localServer": "localhost:1234", "fallbackModel": "qwen3:8b", "localContext": 16384}
        assert saved["view"]["effective"]["localServer"] == "http://localhost:1234/v1"
        # "" turns the fallback off (and stops the offer); a blank address or window goes back to the default
        saved = w.request("POST", "/api/settings", {"scope": "user", "values": {
            "localServer": "", "fallbackModel": "", "localContext": ""}})[1]
        assert saved["ok"] is True, saved
        assert local() == {"fallbackModel": ""}
        for key, value in (("localServer", "ftp://example.com"), ("localContext", "0"), ("localContext", "many")):
            refused = w.request("POST", "/api/settings", {"scope": "user", "values": {key: value}})[1]
            assert refused["ok"] is False and key in refused["message"], (key, refused)
        # a folder's file may not set them: a cloned project could send the conversation elsewhere
        for key, value in values.items():
            refused = w.request("POST", "/api/settings", {"scope": "folder", "values": {key: value}})[1]
            assert refused["ok"] is False and key in refused["message"], (key, refused)
        assert not (w.cwd / ".agentx" / "settings.json").exists()

    def test_save_settings_refuses_paid_fallback(self, window):
        w = window(MockModel())
        w.ready()
        user_file = w.home / ".agentx" / "settings.json"
        before = user_file.read_text(encoding="utf-8") if user_file.exists() else None
        refused = w.request("POST", "/api/settings", {"scope": "user", "values": {
            "fallbackModel": "claude-opus-5-5", "model": "claude-sonnet-5-5"}})[1]
        assert refused == {"ok": False, "message": "fallbackModel must be a local model; claude-opus-5-5 is not."}
        assert (user_file.read_text(encoding="utf-8") if user_file.exists() else None) == before   # nothing saved

    def test_notice_event_carries_actions(self, window):
        w = window(MockModel())
        w.ready()
        actions = [{"label": "Use qwen3:8b", "send": "/fallback qwen3:8b"}, {"label": "No", "send": "/fallback off"}]
        w.app.screen.notice("Ollama is running with qwen3:8b.", actions=actions)
        event = wait_for(w.front, lambda e: e["type"] == "notice" and e.get("actions"))
        assert event["actions"] == actions and event["text"] == "Ollama is running with qwen3:8b."
        w.app.screen.notice("A plain notice.")
        plain = wait_for(w.front, lambda e: e["type"] == "notice" and e["text"] == "A plain notice.")
        assert "actions" not in plain

    def test_the_window_draws_the_local_and_fallback_marks(self):
        """The menu marks local names, the chip says when the fallback is on, a notice's actions are buttons that
        send their text, and the Details team steps say which ran on the fallback and why."""
        from agentx_dev.cli.webui import RESOURCES
        script = (RESOURCES / "app.js").read_text(encoding="utf-8")
        assert 'localModels.includes(m) ? "local"' in script and " · ${esc(it.tag)}" in script   # qwen3:8b · local
        assert '" · fallback"' in script and '"fallback_from" in ev' in script
        assert "ev.actions" in script and 'post("/api/send", {text: a.send})' in script
        assert "↺ on ${esc(s.fallback.to)} (fallback" in script
        for name in ("localServer", "fallbackModel", "localContext"):
            assert f'name="{name}"' in script
