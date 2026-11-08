"""Remote use runs one conversation per device in one process: what they share on disk and on this computer must
take several writers at once."""

import threading

from agentx_dev.cli import mcp, memory, sessions
from agentx_dev.cli.team_record import TeamRecord


def _together(work, count=8):
    threads = [threading.Thread(target=work, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)


def test_titles_written_at_once_are_all_kept(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "project"
    cwd.mkdir()
    _together(lambda n: sessions.save_title(home, cwd, f"2026-11-07-1200{n:02d}-abc.json", f"Title {n}"))
    assert len(sessions.read_titles(home, cwd)) == 8


def test_work_log_entries_written_at_once_are_all_kept(tmp_path):
    _together(lambda n: memory.append_entry(tmp_path, f"## entry {n}\nbody {n}"))
    text = (tmp_path / ".agentx" / "log.md").read_text(encoding="utf-8")
    assert all(f"## entry {n}\nbody {n}" in text for n in range(8))


def test_two_team_jobs_in_one_second_get_their_own_record(tmp_path):
    from datetime import datetime
    when = datetime(2026, 11, 7, 12, 0, 0)
    made = []
    _together(lambda n: made.append(TeamRecord(tmp_path, "same task", when=when).folder), count=4)
    assert None not in made and len(set(made)) == 4


def test_one_mcp_sign_in_at_a_time():
    port = mcp._CallbackPort()
    assert port.take() is True
    assert port.take() is False                    # a second sign-in waits for the first
    port.free()
    assert port.take() is True


# -- one conversation per device, over the real server ----------------------------------------------------------

import http.client
import json
import time

import pytest

from agentx_dev.cli.app import ChatApp
from agentx_dev.cli.webui import WebFront
from tests.conftest import MockModel, make_final, make_react_response

PHONE, LAPTOP = "phone0000001", "laptop000001"


def answers(*texts):
    return MockModel(script=[make_final(text) for text in texts])


class Host:
    """agentx --ui: the main conversation, and one of its own for each other device (``make_device_app``)."""

    def __init__(self, tmp_path, main_model, device_models):
        self.cwd, self.home = tmp_path / "proj", tmp_path / "home"
        self.cwd.mkdir(exist_ok=True)
        self.home.mkdir(exist_ok=True)
        self.front = WebFront(cwd=self.cwd, home=self.home, open_it=False)
        models = list(device_models)

        def make(room):
            model = models.pop(0) if models else answers("fallback")
            return ChatApp(cwd=self.cwd, home=self.home, console=room.console, read_line=room.read,
                           read_short=room.read_short, select=room.select, env={},
                           model_factory=lambda name, env: model)
        self.front.make_device_app = make
        self.front.start_server()
        self.port = self.front._server.server_address[1]
        self.app = ChatApp(cwd=self.cwd, home=self.home, console=self.front.console, read_line=self.front.read,
                           read_short=self.front.read_short, select=self.front.select, env={},
                           model_factory=lambda name, env: main_model)
        self.front.wire(self.app)
        self.thread = threading.Thread(target=self._start, daemon=True)
        self.thread.start()

    def _start(self):
        try:
            self.app.start()
        except EOFError:
            pass

    def request(self, device, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Host": f"127.0.0.1:{self.port}", "X-Agentx-Token": self.front.token}
        if device:
            headers["X-Agentx-Device"] = device
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return json.loads(raw)

    def front_of(self, device):
        return self.front.route(device)

    def close(self):
        self.front.close()
        self.thread.join(5)


def ready(front, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with front._lock:
            if any(r["kind"] == "message" for r in front._pending.values()):
                return True
        time.sleep(0.02)
    raise AssertionError("never ready for a message")


def said(front, text, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(e["type"] == "final" and text in e.get("text", "") for e in front.bus.after(0)):
            return True
        time.sleep(0.02)
    raise AssertionError(f"never said {text!r}")


def until(check, seconds=15.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "never happened"
        time.sleep(0.02)


@pytest.fixture
def host(tmp_path):
    made = []

    def make(main_model, *device_models):
        made.append(Host(tmp_path, main_model, device_models))
        return made[-1]
    yield make
    for h in made:
        h.close()


class TestEachDeviceHasItsOwnConversation:
    def test_the_first_device_has_the_main_one_and_the_next_its_own(self, host):
        h = host(answers("main answer"), answers("phone answer"))
        assert h.front_of(LAPTOP) is h.front                       # the first to connect: the main conversation
        phone = h.front_of(PHONE)
        assert phone is not h.front and phone.host is h.front and h.front_of(PHONE) is phone
        ready(h.front)
        ready(phone)
        assert h.request(PHONE, "POST", "/api/send", {"text": "hello from the phone"})["result"] == "sent"
        said(phone, "phone answer")
        assert not any(e["type"] == "user" and "phone" in e.get("text", "") for e in h.front.bus.after(0))
        assert phone.app is not h.app and phone.app.session_path != h.app.session_path

    def test_two_devices_work_at_the_same_time(self, host):
        slow = MockModel(script=[make_react_response("write_file", {"path": "a.txt", "content": "x",
                                                                    "if_exists": "refuse"}), make_final("main done")])
        h = host(slow, answers("phone done"))
        h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(h.front)
        ready(phone)
        h.request(LAPTOP, "POST", "/api/send", {"text": "write a file"})     # it waits for its approval
        until(lambda: any(r["kind"] == "approval" for r in list(h.front._pending.values())))
        h.request(PHONE, "POST", "/api/send", {"text": "meanwhile"})       # not held up by that approval
        said(phone, "phone done")
        ident = next(i for i, r in list(h.front._pending.items()) if r["kind"] == "approval")
        h.request(LAPTOP, "POST", "/api/answer", {"id": ident, "value": {"choice": "y"}})
        said(h.front, "main done")

    def test_a_device_can_open_the_main_conversation_and_go_back(self, host):
        h = host(answers("main answer"), answers("own answer"))
        h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(h.front)
        state = h.request(PHONE, "GET", "/api/state")
        assert state["live"]["available"] and not state["live"]["joined"]
        assert h.request(PHONE, "POST", "/api/join", {"live": True})["ok"]
        assert h.front_of(PHONE) is h.front and h.request(PHONE, "GET", "/api/state")["live"]["joined"]
        h.request(PHONE, "POST", "/api/send", {"text": "shared question"})
        said(h.front, "main answer")
        h.request(PHONE, "POST", "/api/join", {"live": False})
        assert h.front_of(PHONE) is phone                           # its own conversation is still there
        assert h.request(LAPTOP, "GET", "/api/state")["live"]["available"] is False   # it has the main one

    def test_exit_on_a_device_ends_only_its_conversation(self, host):
        h = host(answers("main answer"), answers("one"), answers("two"))
        h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(phone)
        h.request(PHONE, "POST", "/api/send", {"text": "/exit"})
        until(lambda: phone.ended)
        assert any(e["type"] == "closed" and e.get("room") for e in phone.bus.after(0))
        again = h.front_of(PHONE)
        assert again is not phone and not again.ended                # a new conversation on its next request
        ready(h.front)                                              # the main one goes on

    def test_an_unused_device_conversation_is_put_away_and_reopened(self, host, monkeypatch):
        from agentx_dev.cli import webui
        h = host(answers("main"), answers("first"), answers("again"))
        h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(phone)
        h.request(PHONE, "POST", "/api/send", {"text": "remember me"})
        said(phone, "first")
        ready(phone)
        saved = phone.app.session_path
        monkeypatch.setattr(webui, "ROOM_IDLE_SEC", 0)
        phone._alone_since = time.monotonic() - 1
        h.front._sweep_rooms()
        until(lambda: phone.ended)
        again = h.front_of(PHONE)
        ready(again)
        assert again.app.session_path == saved                      # the same conversation, opened again

    def test_settings_saved_on_one_device_reach_every_conversation(self, host):
        h = host(answers("main"), answers("phone"))
        h.front_of(LAPTOP)
        phone = h.front_of(PHONE)
        ready(phone)
        result = h.request(PHONE, "POST", "/api/settings", {"scope": "folder", "values": {"maxSteps": 7}})
        assert result["ok"]
        assert h.app.max_steps == 7 and phone.app.max_steps == 7

    def test_an_older_page_without_a_device_shares_the_main_one(self, host):
        h = host(answers("main"))
        assert h.front_of("") is h.front and h.front_of("bad id!") is h.front


def test_a_window_says_when_another_conversation_works_in_the_folder(host):
    h = host(answers("main"), answers("phone"))
    h.front_of(LAPTOP)
    phone = h.front_of(PHONE)
    ready(phone)
    ready(h.front)
    assert phone._status()["othersWorking"] == 0
    h.app.progress._shown = True                                   # the main conversation is working
    try:
        assert phone._status()["othersWorking"] == 1
        assert h.front._status()["othersWorking"] == 0             # its own work is not "another"
    finally:
        h.app.progress._shown = False


class TestTailscaleKeepsSharing:
    """The computer slept overnight: Tailscale no longer shared agentx's port, so the iPad's link was dead while the
    terminal still ran, and two earlier runs' entries were still shared, pointing at nothing."""

    STATUS = json.dumps({"Web": {
        "pc.tail.ts.net:10659": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:10659"}}},
        "pc.tail.ts.net:14928": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:14928"}}},
        "pc.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}})

    def test_the_ports_shared_the_agentx_way(self, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "_tailscale", lambda args, timeout=20: (0, self.STATUS))
        assert webui.tailscale_agentx_ports() == {10659, 14928}        # not the user's own :443 -> :3000

    def test_leftovers_with_nothing_behind_them_are_cleared(self, monkeypatch):
        from agentx_dev.cli import webui
        calls = []
        monkeypatch.setattr(webui, "_tailscale", lambda args, timeout=20: calls.append(args) or (0, self.STATUS))
        monkeypatch.setattr(webui, "_listening", lambda port: port == 14928)
        assert webui.tailscale_clear_leftovers(keep=14902) == [10659]   # 14928 still has something behind it
        assert ["serve", "--https=10659", "off"] in calls

    def test_a_lost_share_is_shared_again(self, tmp_path, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "SHARE_CHECK_SEC", 0.05)
        monkeypatch.setattr(webui, "tailscale_agentx_ports", lambda: {10659})   # ours (14902) is gone
        served, said = [], []
        monkeypatch.setattr(webui, "tailscale_serve", lambda port: served.append(port) or "")
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=False, remote=True)
        front.on_window = said.append
        front._serving = 14902
        thread = threading.Thread(target=front._keep_shared, args=(14902,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not served and time.monotonic() < deadline:
            time.sleep(0.02)
        front._serving = None                                          # remote use ends: the check stops
        thread.join(2)
        assert served and served[0] == 14902 and "shared again" in said[0]
        assert not thread.is_alive()
