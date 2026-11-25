"""The start screen of the app window: when agentx --ui starts in a folder that is not a project (the home folder, a
drive root), the window offers the folders the user trusted before instead of a conversation there."""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentx_dev.cli import webui
from agentx_dev.cli.sessions import sessions_dir
from agentx_dev.cli.webui import WebFront, recent_folders, used_ago, wants_start_screen


def _trust(home, folders):
    """The trust file as agentx writes it: comparison keys (lowercased on Windows)."""
    path = home / ".agentx" / "trusted.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"folders": [os.path.normcase(str(f)) for f in folders]}), encoding="utf-8")


def _used(home, folder, when):
    """A conversation saved in ``folder`` at ``when`` (the sessions folder's time is what counts)."""
    target = sessions_dir(home, folder)
    target.mkdir(parents=True, exist_ok=True)
    os.utime(target, (when, when))


@pytest.fixture
def home(tmp_path):
    made = tmp_path / "Home"
    made.mkdir()
    return made


# -- the recent list --------------------------------------------------------------------------------------------

def test_recent_folders_are_sorted_by_last_use_and_unused_ones_last(tmp_path, home):
    now = time.time()
    a, b, c, d = (tmp_path / name for name in ("Alpha", "Beta", "Gamma", "Delta"))
    for folder in (a, b, c, d):
        folder.mkdir()
    _trust(home, [a, b, c, d])
    _used(home, a, now - 3 * 86400)
    _used(home, b, now - 7200)
    _used(home, d, now - 60)
    found = recent_folders(home, now=now)
    assert [f["name"] for f in found] == ["Delta", "Beta", "Alpha", "Gamma"]
    assert [f["ago"] for f in found] == ["1 minute ago", "2 hours ago", "3 days ago", "Not used yet"]
    assert found[0]["path"] == str(d.resolve())


def test_at_most_eight_are_listed(tmp_path, home):
    now = time.time()
    folders = []
    for n in range(11):
        folder = tmp_path / f"p{n:02d}"
        folder.mkdir()
        folders.append(folder)
        _used(home, folder, now - n * 60)
    _trust(home, folders)
    found = recent_folders(home, now=now)
    assert len(found) == 8
    assert [f["name"] for f in found] == [f"p{n:02d}" for n in range(8)]      # the newest eight


def test_missing_folders_files_home_and_roots_are_dropped(tmp_path, home):
    kept, gone, a_file = tmp_path / "Kept", tmp_path / "Gone", tmp_path / "notes.txt"
    kept.mkdir()
    a_file.write_text("x", encoding="utf-8")
    root = Path(Path(tmp_path).anchor)
    _trust(home, [kept, gone, a_file, home, root, kept])        # kept twice: listed once
    found = recent_folders(home)
    assert [f["path"] for f in found] == [str(kept.resolve())]


def test_a_damaged_or_missing_trust_file_is_an_empty_list(home):
    assert recent_folders(home) == []
    (home / ".agentx").mkdir()
    (home / ".agentx" / "trusted.json").write_text("{not json", encoding="utf-8")
    assert recent_folders(home) == []


@pytest.mark.skipif(sys.platform != "win32", reason="the trust file keeps lowercased paths on Windows only")
def test_the_real_spelling_is_restored_on_windows(tmp_path, home):
    folder = tmp_path / "MixedCase" / "MyProject"
    folder.mkdir(parents=True)
    _trust(home, [folder])
    stored = json.loads((home / ".agentx" / "trusted.json").read_text(encoding="utf-8"))["folders"][0]
    assert stored == stored.lower()                       # what the file holds
    found = recent_folders(home)
    assert found[0]["name"] == "MyProject"
    assert found[0]["path"] == str(folder.resolve()) and "MixedCase" in found[0]["path"]


def test_the_ago_wording():
    now = 1_000_000.0
    assert used_ago(None, now) == "Not used yet"
    assert used_ago(now - 10, now) == "Just now"
    assert used_ago(now - 120, now) == "2 minutes ago"
    assert used_ago(now - 3600, now) == "1 hour ago"
    assert used_ago(now - 86400 - 5, now) == "Yesterday"
    assert used_ago(now - 5 * 86400, now) == "5 days ago"
    assert used_ago(now - 21 * 86400, now) == "3 weeks ago"
    assert used_ago(now - 100 * 86400, now) == "3 months ago"
    assert used_ago(now - 800 * 86400, now) == "2 years ago"
    assert used_ago(now + 50, now) == "Just now"           # a clock that moved back


# -- the decision -----------------------------------------------------------------------------------------------

def test_the_start_screen_is_offered_in_the_home_folder_and_a_root_only(tmp_path, home):
    project = tmp_path / "proj"
    project.mkdir()
    assert wants_start_screen(home, home) is True
    assert wants_start_screen(Path(Path(tmp_path).anchor), home) is True
    assert wants_start_screen(project, home) is False
    assert wants_start_screen(home, home, resumed=True) is False     # --continue / --resume
    assert wants_start_screen(home, home, pane=True) is False        # a side of another window
    assert wants_start_screen(home, home, remote=True) is False      # --remote: not this computer's window
    assert wants_start_screen(home, home, no_open=True) is False     # --no-open: perhaps through a tunnel


def _front(tmp_path, home, *, offered=True):
    front = WebFront(cwd=home, home=home, open_it=False)
    front.start_screen = offered
    return front


def test_the_view_says_what_to_show(tmp_path, home):
    project = tmp_path / "Shop"
    project.mkdir()
    _trust(home, [project])
    view = _front(tmp_path, home).start_view()
    assert view["show"] is True
    assert view["line"] == f"{home} is your home folder, not a project."
    assert [f["name"] for f in view["recent"]] == ["Shop"]
    assert _front(tmp_path, home, offered=False).start_view() == {"show": False}


def test_a_device_room_or_a_lane_never_shows_it(tmp_path, home):
    front = _front(tmp_path, home)
    room = WebFront(cwd=home, home=home, open_it=False)
    room.start_screen = True
    room.host = front                                     # another device's conversation, or a lane
    assert room.start_view() == {"show": False}


def test_a_conversation_with_messages_never_shows_it(tmp_path, home):
    front = _front(tmp_path, home)

    class App:
        class session:
            history = [{"role": "user", "content": "hello"}]
    front.app = App()
    assert front.start_view() == {"show": False}


def test_chat_here_hides_it_for_this_run(tmp_path, home):
    front = _front(tmp_path, home)
    assert front.start_choice("here")["ok"] is True
    assert front.start_view() == {"show": False}           # a reload shows the welcome now


# -- choosing a folder ------------------------------------------------------------------------------------------

def test_choosing_a_folder_starts_agentx_there_and_ends_this_one(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append((a, k)))
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 1.0)
    front = _front(tmp_path, home)
    result = front.start_choice("open", str(project))
    assert result["ok"] is True
    assert result["message"] == f"agentx opened {project.resolve()} in a new window. You can close this one."
    (command,), kwargs = started[0]
    assert command == webui.open_folder_command(project.resolve())[0]     # the "Open another folder" start
    assert front.start_view() == {"show": False}
    assert front.handed_over() is False                    # a few seconds later, not at once
    time.sleep(1.1)
    assert front.handed_over() is True


def test_a_folder_that_cannot_be_opened_keeps_the_screen(tmp_path, home, monkeypatch):
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append((a, k)))
    front = _front(tmp_path, home)
    result = front.start_choice("open", str(tmp_path / "nowhere"))
    assert result["ok"] is False and "is not a folder" in result["message"]
    assert not started and front.start_view()["show"] is True
    assert front.handed_over() is False


def test_nothing_is_started_once_the_screen_is_gone(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append((a, k)))
    front = _front(tmp_path, home)
    front.start_choice("here")
    assert front.start_choice("open", str(project))["ok"] is False
    assert _front(tmp_path, home, offered=False).start_choice("open", str(project))["ok"] is False
    assert front.start_choice("bogus")["ok"] is False
    assert not started


def test_the_run_ends_after_the_hand_over_and_the_window_is_told(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 0.3)
    front = _front(tmp_path, home)

    def work():                                            # as agentx --ui runs it
        return 0 if not front.wait_for_start() else 5
    box = {}
    runner = threading.Thread(target=lambda: box.update(code=front.run(work)), daemon=True)
    runner.start()
    deadline = time.monotonic() + 10
    while front._server is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert front.start_choice("open", str(project))["ok"] is True
    runner.join(15)
    assert not runner.is_alive() and box["code"] == 0
    closed = [e for e in front.bus.after(0) if e["type"] == "closed"]
    assert closed and closed[0]["handedOff"] == str(project.resolve())


# -- nothing happens in this folder until the start screen is answered -------------------------------------------

def _waiting(front):
    box = {}
    thread = threading.Thread(target=lambda: box.update(go=front.wait_for_start()), daemon=True)
    thread.start()
    time.sleep(0.3)
    return thread, box


def test_the_app_waits_for_chat_here(tmp_path, home):
    front = _front(tmp_path, home)
    thread, box = _waiting(front)
    assert thread.is_alive()                               # no trust question, no app yet
    front.start_choice("here")
    thread.join(5)
    assert box == {"go": True}
    assert _front(tmp_path, home, offered=False).wait_for_start() is True     # a project: straight on


def test_the_app_is_never_built_after_a_folder_was_chosen(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 0.3)
    front = _front(tmp_path, home)
    thread, box = _waiting(front)
    front.start_choice("open", str(project))
    thread.join(5)
    assert box == {"go": False}


def test_closing_ends_the_wait(tmp_path, home):
    front = _front(tmp_path, home)
    thread, box = _waiting(front)
    front.close()
    thread.join(5)
    assert box == {"go": False}


# -- over the window's API --------------------------------------------------------------------------------------

def _request(front, method, path, body=None):
    port = front._server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Host": f"127.0.0.1:{port}", "X-Agentx-Token": front.token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    raw = conn.getresponse().read()
    conn.close()
    return json.loads(raw)


def test_the_api_shows_it_and_chat_here_hides_it(tmp_path, home):
    project = tmp_path / "Shop"
    project.mkdir()
    _trust(home, [project])
    front = _front(tmp_path, home)
    front.start_server()
    try:
        view = _request(front, "GET", "/api/start")
        assert view["show"] is True and view["recent"][0]["path"] == str(project.resolve())
        assert _request(front, "POST", "/api/start", {"action": "here"})["ok"] is True
        assert _request(front, "GET", "/api/start") == {"show": False}
    finally:
        front.close()


@pytest.mark.parametrize("where, resume, no_open, shown", [("home", None, False, True),
                                                           ("project", None, False, False),
                                                           ("home", "latest", False, False),
                                                           ("home", None, True, False)])
def test_agentx_ui_decides_at_start(tmp_path, home, monkeypatch, where, resume, no_open, shown):
    import argparse
    from agentx_dev.cli import app as app_module
    project = tmp_path / "proj"
    project.mkdir()
    seen = {}

    class Front:
        def __init__(self, **kw):
            self.app = None

        def run(self, work):
            seen["front"] = self
            return 0
    monkeypatch.setattr(webui, "WebFront", Front)
    monkeypatch.setattr(app_module, "setup_logging", lambda home: None)
    monkeypatch.setattr(app_module, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(home if where == "home" else project)
    args = argparse.Namespace(port=0, no_open=no_open, remote=False, continue_=False, resume=resume)
    assert app_module._run_ui(args) == 0
    assert seen["front"].start_screen is shown



# -- review fixes ---------------------------------------------------------------------------------------------------

class _Process:
    """A started agentx, as Popen returns it: ``code`` is what ``poll`` says (None while it runs)."""

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def test_a_new_agentx_that_ended_at_once_brings_the_screen_back(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Process(1))
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 0.2)
    front = _front(tmp_path, home)
    thread, box = _waiting(front)                          # the app waits, as agentx --ui runs it
    assert front.start_choice("open", str(project))["ok"] is True
    time.sleep(0.6)
    assert front.handed_over() is False                    # it did not start: this agentx stays
    assert thread.is_alive()
    view = front.start_view()
    assert view["show"] is True
    assert view["problem"] == f"agentx could not start in {project.resolve()} (exit 1)."
    failed = [e for e in front.bus.after(0) if e["type"] == "start_failed"]
    assert len(failed) == 1 and failed[0]["message"] == view["problem"]
    assert front.start_choice("here")["ok"] is True        # and the screen works again
    thread.join(5)
    assert box == {"go": True}


def test_the_run_stays_when_the_new_agentx_failed(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Process(2))
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 0.2)
    front = _front(tmp_path, home)
    box = {}
    runner = threading.Thread(target=lambda: box.update(code=front.run(lambda: 7 if front.wait_for_start() else 0)),
                              daemon=True)
    runner.start()
    deadline = time.monotonic() + 10
    while front._server is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert front.start_choice("open", str(project))["ok"] is True
    time.sleep(1.5)                                        # past the hand-over and a turn of the run loop
    assert runner.is_alive()
    assert not [e for e in front.bus.after(0) if e["type"] == "closed"]
    front.start_choice("here")
    runner.join(10)
    assert box["code"] == 7


def test_a_still_running_agentx_is_handed_over(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Process(None))
    monkeypatch.setattr(webui, "HAND_OVER_SEC", 0.1)
    front = _front(tmp_path, home)
    front.start_choice("open", str(project))
    time.sleep(0.3)
    assert front.handed_over() is True and front.handed_over() is True


def test_two_choices_at_once_start_one_agentx(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    started = []

    def slow(*a, **k):
        started.append(a)
        time.sleep(0.3)
        return _Process(None)
    monkeypatch.setattr(subprocess, "Popen", slow)
    front = _front(tmp_path, home)
    results = []
    threads = [threading.Thread(target=lambda: results.append(front.start_choice("open", str(project))))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(started) == 1
    assert sorted(r["ok"] for r in results) == [False, True]


def test_here_while_a_folder_is_starting_is_refused(tmp_path, home, monkeypatch):
    project = tmp_path / "Shop"
    project.mkdir()
    gate = threading.Event()

    def held(*a, **k):
        gate.wait(5)
        return _Process(None)
    monkeypatch.setattr(subprocess, "Popen", held)
    front = _front(tmp_path, home)
    opener = threading.Thread(target=lambda: front.start_choice("open", str(project)))
    opener.start()
    time.sleep(0.2)
    assert front.start_choice("here")["ok"] is False
    assert front._start_chosen is False
    gate.set()
    opener.join(5)


def test_while_it_waits_nothing_runs_in_the_folder(tmp_path, home, monkeypatch):
    from agentx_dev.cli import background
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    project = tmp_path / "Shop"
    project.mkdir()
    front = _front(tmp_path, home)

    def no_room(room):
        raise AssertionError("no room while it waits")
    front.make_device_app = no_room
    before = dict(background._shared)
    front.start_server()
    try:
        port = front._server.server_address[1]

        def call(method, path, body=None, device="aaaaaaaa11"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            headers = {"Host": f"127.0.0.1:{port}", "X-Agentx-Token": front.token, "X-Agentx-Device": device}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            out = response.status, json.loads(response.read())
            conn.close()
            return out
        assert call("GET", "/api/start")[0] == 200            # the window's own device
        assert call("GET", "/api/state")[0] == 200
        for path in ("/api/tasks", "/api/files?q=x", "/api/browser/state", "/api/details"):
            assert call("GET", path)[0] == 409, path
        for path, body in (("/api/terminals", {}), ("/api/open-folder", {"path": str(project)}),
                           ("/api/pane", {"folder": str(project)}), ("/api/send", {"text": "hi"}),
                           ("/api/lanes", {}), ("/api/tasks/clear", {})):
            status, got = call("POST", path, body)
            assert status == 409 and got["ok"] is False, path
        assert call("GET", "/api/state", device="bbbbbbbb22")[0] == 409     # another device's window
        assert not front.rooms and not started
        assert background._shared == before                 # no task manager made without the app
        assert call("POST", "/api/start", {"action": "here"})[0] == 200
        status, got = call("GET", "/api/tasks")              # chosen, but the app is not built yet
        assert status == 200 and got["tasks"] == [] and got["error"] == webui.NOT_READY
        assert call("POST", "/api/terminals", {})[1]["ok"] is False
        assert background._shared == before
    finally:
        front.close()
