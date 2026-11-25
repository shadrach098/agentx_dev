"""The window's Tasks panel, over the real server: background tasks (list, output, stop, clear), terminals (open,
type, Ctrl+C, close), the browser (state, frames, take over, the user's own input), and what remote control turns
off. Real processes and, when it is there, a real Chromium; every wait has a deadline."""
import http.client
import http.server
import json
import sys
import threading
import time

import pytest

from agentx_dev.cli.browser import NOT_INSTALLED, BrowserHost
from agentx_dev.cli.sessions import save_title, short_id
from tests.test_one_conversation_per_device import LAPTOP, PHONE, Host, answers, ready

PY = sys.executable.replace("\\", "/")
TERMINALS_OFF = "terminals are off while remote control is on"


@pytest.fixture
def host(tmp_path):
    made = []

    def make(*device_models):
        made.append(Host(tmp_path, answers("main"), device_models))
        return made[-1]
    yield make
    for h in made:
        tasks = getattr(h.app, "tasks", None)
        h.close()
        if tasks is not None:
            tasks.stop_all()                                # leave nothing running, whatever the test did


def raw(h, device, method, path, body=None, token=True):
    """``(status, headers, bytes)`` of one request, the way the window makes it."""
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=30)
    headers = {"Host": f"127.0.0.1:{h.port}"}
    if token:
        headers["X-Agentx-Token"] = h.front.token
    if device:
        headers["X-Agentx-Device"] = device
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    out = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    conn.close()
    return out


def until(check, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("never happened")


def script(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f'"{PY}" "{str(f).replace(chr(92), "/")}"'


def reader(h, tid, device=LAPTOP):
    """Reads a task's output through the window, the way its panel polls: everything gathered so far."""
    seen = {"text": "", "offset": 0}

    def more():
        got = h.request(device, "GET", f"/api/tasks/output?id={tid}&after={seen['offset']}")
        assert got["offset"] >= seen["offset"]
        seen["text"] += got["text"]
        seen["offset"] = got["offset"]
        seen["state"] = got["state"]
        return seen
    return seen, more


def rows(h, device=LAPTOP):
    return {row["id"]: row for row in h.request(device, "GET", "/api/tasks")["tasks"]}


class TestTasks:
    def test_list_output_stop_and_clear(self, host, tmp_path):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        owner = main.conversation_id()
        assert owner
        save_title(h.home, h.cwd, f"20261124T120000Z-{short_id(owner)}.json", "Apples chat")
        task = main.app.tasks.start(script(tmp_path, "srv.py", "import time\nprint('hello-task', flush=True)\n"
                                                                "time.sleep(60)\n"),
                                    owner=owner, name="dev\x1b[31m server")
        listed = h.request(LAPTOP, "GET", "/api/tasks")
        assert listed["terminalsAllowed"] is True
        row = {r["id"]: r for r in listed["tasks"]}[task.id]
        assert set(row) == {"id", "kind", "name", "command", "state", "exitCode", "startedAt", "endedAt", "owner",
                            "ownerTitle"}
        assert (row["kind"], row["state"], row["exitCode"], row["endedAt"]) == ("task", "running", None, None)
        assert row["owner"] == owner and row["ownerTitle"] == "Apples chat"
        assert "\x1b" not in row["name"] and "\\x1b" in row["name"]       # names go out made safe to show
        assert "srv.py" in row["command"] and row["startedAt"] > 0
        seen, more = reader(h, task.id)
        until(lambda: "hello-task" in more()["text"])
        assert seen["state"] == "running"
        again = h.request(LAPTOP, "GET", f"/api/tasks/output?id={task.id}&after={seen['offset']}")
        assert again["text"] == "" and again["offset"] == seen["offset"]
        assert h.request(LAPTOP, "POST", "/api/tasks/stop", {"id": task.id}) == {"ok": True}
        row = rows(h)[task.id]
        assert row["state"] == "stopped" and row["endedAt"]
        assert h.request(LAPTOP, "POST", "/api/tasks/clear", {})["ok"] is True
        assert task.id not in rows(h)

    def test_an_unknown_task_is_refused(self, host):
        h = host()
        ready(h.front_of(LAPTOP))
        assert h.request(LAPTOP, "POST", "/api/tasks/stop", {"id": "t99"})["ok"] is False
        got = h.request(LAPTOP, "GET", "/api/tasks/output?id=t99&after=0")
        assert got["text"] == "" and got["error"]

    def test_a_prompt_without_a_newline_reaches_the_window(self, host, tmp_path):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        task = main.app.tasks.start(script(tmp_path, "ask.py", "import sys, time\nsys.stdout.write('Name: ')\n"
                                                                "sys.stdout.flush()\ntime.sleep(60)\n"))
        seen, more = reader(h, task.id)
        until(lambda: more()["text"] == "Name: ")
        assert seen["offset"] == len(b"Name: ")
        time.sleep(0.3)
        assert more()["text"] == "Name: "                       # handed out once, not again

    def test_the_status_counts_running_tasks(self, host, tmp_path):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        assert main._status()["tasks"] == 0
        task = main.app.tasks.start(script(tmp_path, "s.py", "import time; time.sleep(60)"))
        term = main.app.tasks.open_terminal()
        assert main._status()["tasks"] == 2
        main.app.tasks.stop(task.id)
        main.app.tasks.stop(term.id)
        assert main._status()["tasks"] == 0

    def test_a_front_without_an_app_does_not_make_a_manager(self, tmp_path):
        from agentx_dev.cli import background
        from agentx_dev.cli.webui import WebFront
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        before = dict(background._shared)
        assert "tasks" not in front._status()
        assert background._shared == before


class TestTerminals:
    def test_open_type_interrupt_and_close(self, host, tmp_path):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        opened = h.request(LAPTOP, "POST", "/api/terminals", {})
        assert opened["ok"] is True
        tid = opened["id"]
        row = rows(h)[tid]
        assert row["kind"] == "terminal" and row["state"] == "running" and row["owner"] == ""
        assert h.request(LAPTOP, "POST", "/api/terminals/input", {"id": tid, "text": "echo hi"}) == {"ok": True}
        seen, more = reader(h, tid)
        until(lambda: "hi" in more()["text"].replace("$ echo hi", ""))
        sleeper = tmp_path / "long.py"
        sleeper.write_text("import time\nprint('long-started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
        path = str(sleeper).replace("\\", "/")
        command = f'& "{PY}" "{path}"' if sys.platform == "win32" else f'"{PY}" "{path}"'
        assert h.request(LAPTOP, "POST", "/api/terminals/input", {"id": tid, "text": command})["ok"]
        until(lambda: "long-started" in more()["text"])
        assert h.request(LAPTOP, "POST", "/api/terminals/interrupt", {"id": tid}) == {"ok": True}
        assert h.request(LAPTOP, "POST", "/api/terminals/input", {"id": tid, "text": "echo after-interrupt"})["ok"]
        until(lambda: "after-interrupt" in more()["text"].replace("$ echo after-interrupt", ""))
        assert h.request(LAPTOP, "POST", "/api/terminals/close", {"id": tid}) == {"ok": True}
        assert rows(h)[tid]["state"] == "stopped"
        refused = h.request(LAPTOP, "POST", "/api/terminals/input", {"id": tid, "text": "echo late"})
        assert refused["ok"] is False and refused["error"]

    def test_a_task_is_not_a_terminal(self, host, tmp_path):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        task = main.app.tasks.start(script(tmp_path, "s.py", "import time; time.sleep(60)"))
        for path, body in (("/api/terminals/input", {"id": task.id, "text": "x"}),
                           ("/api/terminals/interrupt", {"id": task.id}),
                           ("/api/terminals/close", {"id": task.id})):
            got = h.request(LAPTOP, "POST", path, body)
            assert got["ok"] is False and "not a terminal" in got["error"], path
        assert main.app.tasks.get(task.id).state == "running"

    def test_too_many_terminals_says_so(self, host, monkeypatch):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        monkeypatch.setattr(main.app.tasks, "max_terminals", 1)
        assert h.request(LAPTOP, "POST", "/api/terminals", {})["ok"]
        second = h.request(LAPTOP, "POST", "/api/terminals", {})
        assert second["ok"] is False and "term1" in second["error"]


class TestRemoteControl:
    def test_terminals_and_take_over_are_off(self, host, tmp_path):
        h = host(answers("phone"))
        main = h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(main)
        ready(phone)
        term = main.app.tasks.open_terminal()                  # opened before remote control came on
        task = main.app.tasks.start(script(tmp_path, "s.py", "import time; time.sleep(60)"))
        main.app.browser = BrowserHost()                        # a browser of its own: nothing here starts it
        phone.app.browser = main.app.browser
        h.front.remote = True                                   # /remote-control
        for device in (LAPTOP, PHONE):
            assert h.request(device, "GET", "/api/tasks")["terminalsAllowed"] is False
            for path, body in (("/api/terminals", {}), ("/api/terminals/input", {"id": term.id, "text": "echo x"}),
                               ("/api/terminals/interrupt", {"id": term.id}), ("/api/terminals/close", {"id": term.id}),
                               ("/api/tasks/stop", {"id": term.id})):
                assert h.request(device, "POST", path, body) == {"ok": False, "error": TERMINALS_OFF}, (device, path)
            state = h.request(device, "GET", "/api/browser/state")
            assert state["takeAllowed"] is False
            take = h.request(device, "POST", "/api/browser/take", {})
            assert take["ok"] is False and "remote control" in take["error"]
            typed = h.request(device, "POST", "/api/browser/input", {"event": {"type": "navigate",
                                                                               "url": "http://127.0.0.1:1/"}})
            assert typed["ok"] is False and "remote control" in typed["error"]
        assert main.app.tasks.get(term.id).state == "running"
        assert len(main.app.tasks.list()) == 2
        assert h.request(PHONE, "GET", f"/api/tasks/output?id={term.id}&after=0")["state"] == "running"  # it may watch
        assert h.request(PHONE, "POST", "/api/tasks/stop", {"id": task.id}) == {"ok": True}   # a task may be stopped
        assert main.app.browser._thread is None                 # Chromium was never started
        h.front.remote = False
        assert h.request(LAPTOP, "GET", "/api/tasks")["terminalsAllowed"] is True
        assert h.request(LAPTOP, "POST", "/api/terminals/close", {"id": term.id}) == {"ok": True}


class TestBrowser:
    def test_without_playwright_it_says_how_to_install(self, host, monkeypatch):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        main.app.browser = BrowserHost()
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        state = h.request(LAPTOP, "GET", "/api/browser/state")
        assert set(state) == {"available", "open", "url", "title", "user", "frame", "takeAllowed", "error"}
        assert state["available"] is False and state["error"] == NOT_INSTALLED
        assert state["open"] is False and state["takeAllowed"] is True
        status, headers, body = raw(h, LAPTOP, "GET", "/api/browser/frame?since=0")
        assert status == 204 and body == b""
        typed = h.request(LAPTOP, "POST", "/api/browser/input", {"event": {"type": "navigate",
                                                                           "url": "http://127.0.0.1:1/"}})
        assert typed["ok"] is False and typed["error"] == NOT_INSTALLED
        assert main.app.browser._thread is None or main.app.browser._browser is None

    def test_the_frame_needs_the_key(self, host):
        h = host()
        ready(h.front_of(LAPTOP))
        status, _, _ = raw(h, LAPTOP, "GET", "/api/browser/frame?since=0", token=False)
        assert status == 403

    def test_a_page_shown_live_taken_over_and_driven(self, host):
        from tests.test_browser_host import _launch_problem, _Pages
        problem = _launch_problem()
        if problem:
            pytest.skip(problem)
        site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
        site.daemon_threads = True
        threading.Thread(target=site.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{site.server_address[1]}"
        browser = BrowserHost()
        try:
            h = host()
            main = h.front_of(LAPTOP)
            ready(main)
            main.app.browser = browser
            owner = main.conversation_id()
            browser.open(owner, base + "/next")
            state = h.request(LAPTOP, "GET", "/api/browser/state")
            assert state["available"] is True and state["open"] is True and state["error"] == ""
            assert state["url"] == base + "/next" and state["title"] == "Next"
            status, headers, body = raw(h, LAPTOP, "GET", "/api/browser/frame?since=0")
            assert status == 200 and body[:2] == b"\xff\xd8"
            assert headers["content-type"] == "image/jpeg" and headers["cache-control"] == "no-store"
            shown = int(headers["x-frame"])
            assert shown >= 1
            status, headers, body = raw(h, LAPTOP, "GET", f"/api/browser/frame?since={shown}")
            assert status == 204 and body == b"" and int(headers["x-frame"]) == shown
            assert h.request(LAPTOP, "POST", "/api/browser/take", {}) == {"ok": True}
            assert h.request(LAPTOP, "GET", "/api/browser/state")["user"] is True and browser.held_by_user(owner)
            moved = h.request(LAPTOP, "POST", "/api/browser/input", {"event": {"type": "navigate",
                                                                               "url": base + "/other"}})
            assert moved == {"ok": True}
            assert h.request(LAPTOP, "GET", "/api/browser/state")["url"] == base + "/other"
            refused = h.request(LAPTOP, "POST", "/api/browser/input", {"event": {"type": "navigate",
                                                                                 "url": "file:///C:/Windows/win.ini"}})
            assert refused["ok"] is False and refused["error"]
            bad = h.request(LAPTOP, "POST", "/api/browser/input", {"event": "click"})
            assert bad["ok"] is False
            assert h.request(LAPTOP, "POST", "/api/browser/give", {}) == {"ok": True}
            assert not browser.held_by_user(owner)
            other = h.request(PHONE, "GET", "/api/browser/state")          # another conversation: not this page
            assert other["open"] is False
        finally:
            browser.close()
            site.shutdown()
            site.server_close()

    def test_a_click_with_no_page_open_does_not_start_the_browser(self, host):
        h = host()
        main = h.front_of(LAPTOP)
        ready(main)
        main.app.browser = BrowserHost()
        got = h.request(LAPTOP, "POST", "/api/browser/input", {"event": {"type": "click", "x": 5, "y": 5}})
        assert got["ok"] is False and got["error"]
        assert main.app.browser._thread is None
