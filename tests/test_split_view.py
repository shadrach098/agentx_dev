"""The split view: a conversation beside this one, run by an agentx of its own in a folder, drawn in the window.
The agentx is started like "Open another folder" (nothing from that folder is imported first), opens no window of
its own, hands its one-time link back through a private file, and quits when its side is closed or never shown."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agentx_dev.cli import webui
from agentx_dev.cli.webui import WebFront, pane_command, pane_handoff_opener


def front_in(tmp_path, app=None):
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    front = WebFront(cwd=cwd, home=tmp_path / "home", open_it=False)
    front.app = app
    return front


class TestStartingASide:
    def test_the_command_resumes_the_conversation_in_its_folder_without_importing_from_it(self, tmp_path):
        command, start_in = pane_command(tmp_path, "20261030T002649Z-611a7fdf")
        assert command[0] == sys.executable and "-c" in command and str(tmp_path) in command
        assert command[-2:] == ["--resume", "20261030T002649Z-611a7fdf"]
        code = command[command.index("-c") + 1]
        assert "args = ['--ui'] + sys.argv[3:]" in code and "os.chdir(sys.argv[1])" in code
        assert Path(start_in) != tmp_path                     # started in agentx's own folder, then moves
        assert pane_command(tmp_path)[0][-1] == str(Path(webui._agentx_root()))   # a new conversation: no --resume

    def test_the_link_goes_to_a_private_file_not_a_browser(self, tmp_path):
        target = tmp_path / "link"
        assert pane_handoff_opener(str(target))("http://127.0.0.1:5000/?c=abc") == "pane"
        assert target.read_text(encoding="utf-8") == "http://127.0.0.1:5000/?c=abc"
        if os.name != "nt":
            assert (target.stat().st_mode & 0o777) == 0o600

    def test_a_conversation_is_opened_beside_with_its_title(self, tmp_path, monkeypatch):
        front = front_in(tmp_path, app=NS(session=NS(session_id="current-one")))
        found = [NS(session_id="other-one", title="deploy to vps", path=tmp_path / "x.json")]
        monkeypatch.setattr("agentx_dev.cli.sessions.list_sessions", lambda home, folder: found)
        started = []
        monkeypatch.setattr(front, "_start_pane", lambda folder, session: (started.append((folder, session)) or
                                                                           ("http://127.0.0.1:5001/?c=xyz", "")))
        answer = front.open_pane("other-one")
        assert answer["ok"] and answer["url"] == "http://127.0.0.1:5001/?c=xyz" and answer["title"] == "deploy to vps"
        assert started == [(front.cwd.resolve(), "other-one")] and answer["session"] == "other-one"

    def test_what_cannot_be_opened_beside_is_refused(self, tmp_path, monkeypatch):
        front = front_in(tmp_path, app=NS(session=NS(session_id="current-one")))
        monkeypatch.setattr("agentx_dev.cli.sessions.list_sessions",
                            lambda home, folder: [NS(session_id="current-one", title="t", path=tmp_path / "c.json")])
        monkeypatch.setattr(front, "_start_pane", lambda *a: pytest.fail("nothing may start"))
        assert "already open here" in front.open_pane("current-one")["message"]
        assert "not found" in front.open_pane("missing")["message"]
        assert not front.open_pane("../../etc/passwd")["ok"]
        assert "is not a folder" in front.open_pane("", str(tmp_path / "nowhere"))["message"]

    def test_another_folder_opens_a_new_conversation_beside(self, tmp_path, monkeypatch):
        other = tmp_path / "shop"
        other.mkdir()
        front = front_in(tmp_path)
        monkeypatch.setattr(front, "_start_pane", lambda folder, session: ("http://127.0.0.1:5002/?c=q", ""))
        answer = front.open_pane("", str(other))
        assert answer["ok"] and answer["folderName"] == "shop" and answer["title"] == "New conversation"

    def test_a_side_that_did_not_start_says_why(self, tmp_path, monkeypatch):
        front = front_in(tmp_path)
        monkeypatch.setattr(front, "_start_pane", lambda folder, session: ("", "No API key found"))
        answer = front.open_pane("", str(front.cwd))
        assert not answer["ok"] and answer["message"].endswith(": No API key found")

    def test_the_route_is_served(self):
        source = Path(webui.__file__).read_text(encoding="utf-8")
        assert 'url.path == "/api/pane"' in source and "frame-src http://127.0.0.1:*" in source
        assert "frame-ancestors" not in source          # a page shown inside another viewer (a preview) still loads


class TestASideQuitsByItself:
    def test_a_side_whose_window_never_came_quits(self, tmp_path, monkeypatch):
        front = front_in(tmp_path)
        front.open_it = True
        front.pane_started = time.monotonic() - webui.PANE_ORPHAN_SEC - 1
        assert front.window_closed()
        front.pane_started = time.monotonic()
        assert not front.window_closed()

    def test_the_window_started_as_a_side_hands_its_link_back(self, monkeypatch, tmp_path):
        from agentx_dev.cli import app as app_module
        seen = {}

        class Front:
            def __init__(self, **kw):
                seen.update(kw)
                self.opener = None
                self.pane_started = None

            def run(self, work):
                seen["front"] = self
                return 0
        monkeypatch.setattr(webui, "WebFront", Front)
        monkeypatch.setenv("AGENTX_PANE_HANDOFF", str(tmp_path / "link"))
        args = NS(port=0, no_open=False, remote=False)
        assert app_module._run_ui(args) == 0
        front = seen["front"]
        assert seen["open_it"] is True and front.pane_started is not None
        assert front.opener("http://127.0.0.1:1/?c=z") == "pane" and (tmp_path / "link").is_file()
        assert "AGENTX_PANE_HANDOFF" not in os.environ          # never passed on to its tools' shells


def test_the_window_has_the_split_view():
    from agentx_dev.cli.webui import RESOURCES
    html = (RESOURCES / "index.html").read_text(encoding="utf-8")
    script = (RESOURCES / "app.js").read_text(encoding="utf-8")
    css = (RESOURCES / "style.css").read_text(encoding="utf-8")
    assert 'id="beside"' in html and 'id="besideFrame"' in html and 'id="dropBeside"' in html
    assert "async function openBeside" in script and 'post("/api/pane"' in script and "function closeBeside" in script
    assert 'draggable="${!s.current && !EMBED}"' in script and "data-beside=" in script
    assert "Open a folder beside…" in script and "const EMBED" in script
    assert ".app.split {" in css and "html.embedded .side" in css
