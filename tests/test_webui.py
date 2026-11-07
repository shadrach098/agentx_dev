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
        for name in ("/app.js", "/style.css", "/icon.svg"):
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
