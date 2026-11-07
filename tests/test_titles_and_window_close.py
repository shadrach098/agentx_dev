"""Conversation titles written by the model, and the app window quitting agentx when it is closed."""

import io
import json
import time

from rich.console import Console

from agentx_dev.cli.app import ChatApp, clean_title
from agentx_dev.cli.sessions import list_sessions, read_titles, save_title, user_messages
from agentx_dev.cli.webui import WebFront
from tests.conftest import MockModel, make_final


class TestTitles:
    def test_clean_title(self):
        assert clean_title('"Fix the invite link sign-in."') == "Fix the invite link sign-in"
        assert clean_title("Title: **Supabase table count**\nmore") == "Supabase table count"
        assert clean_title("\n\n# Flight search with MCP\n") == "Flight search with MCP"
        assert clean_title("") == "" and clean_title("  \n ") == ""
        assert len(clean_title("word " * 40)) <= 60

    def test_what_the_person_said_skips_agentx_notes_and_summaries(self):
        history = [
            {"role": "user", "content": "[Summary of the earlier conversation, written by agentx]\nlots"},
            {"role": "user", "content": "fix the invite link\n\n<project-notes>notes</project-notes>"},
            {"role": "user", "content": "Observation: 3 files"},
            {"role": "user", "content": [{"type": "text", "text": "and the reset page"}]},
        ]
        assert user_messages(history) == ["fix the invite link", "and the reset page"]

    def test_a_saved_title_wins_over_the_first_message(self, tmp_path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        cwd.mkdir()
        app = ChatApp(cwd=cwd, home=home, console=Console(file=io.StringIO(), width=100), read_line=lambda *a, **k: "", env={},
                      model_factory=lambda name, env: MockModel(script=[make_final("ok")]))
        assert app is not None
        folder = home / ".agentx" / "sessions"
        from agentx_dev.cli.sessions import sessions_dir
        target = sessions_dir(home, cwd)
        target.mkdir(parents=True)
        (target / "20261101T100000Z-abc.json").write_text(json.dumps(
            {"session_id": "abc", "updated_at": "2026-11-01T10:00:00", "history": [
                {"role": "user", "content": "lets continue"}, {"role": "assistant", "content": "ok"}]}),
            encoding="utf-8")
        assert [s.title for s in list_sessions(home, cwd)] == ["lets continue"]
        assert save_title(home, cwd, "20261101T100000Z-abc.json", "Invite link sign-in fix") is True
        assert [s.title for s in list_sessions(home, cwd)] == ["Invite link sign-in fix"]
        assert folder.exists()

    def test_the_model_writes_the_title_once_after_the_first_answer(self, tmp_path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        cwd.mkdir()
        made = []

        def factory(name, env):
            # the chat model first, then a fresh one for the title
            made.append(MockModel(script=[make_final("Done: the link works")] if not made else ["Invite link fix"]))
            return made[-1]

        answers = iter(["fix the invite link", "/exit"])
        app = ChatApp(cwd=cwd, home=home, console=Console(file=io.StringIO(), width=100), read_line=lambda *a, **k: next(answers), env={},
                      model_factory=factory)
        app.auto_titles = True
        app.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not read_titles(home, cwd):
            time.sleep(0.05)
        titles = read_titles(home, cwd)
        assert list(titles.values()) == ["Invite link fix"]
        assert [s.title for s in list_sessions(home, cwd)] == ["Invite link fix"]
        assert len(made) == 2

    def test_no_title_call_without_auto_titles(self, tmp_path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        cwd.mkdir()
        made = []

        def factory(name, env):
            made.append(MockModel(script=[make_final("ok")]))
            return made[-1]

        answers = iter(["hello", "/exit"])
        ChatApp(cwd=cwd, home=home, console=Console(file=io.StringIO(), width=100), read_line=lambda *a, **k: next(answers), env={},
                model_factory=factory).start()
        time.sleep(0.2)
        assert len(made) == 1 and read_titles(home, cwd) == {}


class TestClosingTheWindow:
    def test_closed_only_after_the_grace_and_only_for_a_window_agentx_opened(self, tmp_path, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "CLOSE_GRACE_SEC", 0.2)
        front = WebFront(cwd=tmp_path, home=tmp_path, open_it=True, opener=lambda url: "")
        assert front.window_closed() is False              # never connected yet (the trust question, say)
        front.window_connected(True)
        front.window_connected(False)
        assert front.window_closed() is False              # a reload comes back within the grace
        front.window_connected(True)
        front.window_connected(False)
        time.sleep(0.25)
        assert front.window_closed() is True
        front.window_connected(True)
        assert front.window_closed() is False

        kept = WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
        kept.window_connected(True)
        kept.window_connected(False)
        time.sleep(0.25)
        assert kept.window_closed() is False                # --no-open keeps running for a later connection
        remote = WebFront(cwd=tmp_path, home=tmp_path, open_it=True, remote=True)
        assert remote.open_it is False

    def test_closing_the_window_ends_the_app(self, tmp_path, monkeypatch):
        from agentx_dev.cli import webui
        monkeypatch.setattr(webui, "CLOSE_GRACE_SEC", 0.3)
        front = WebFront(cwd=tmp_path / "proj", home=tmp_path / "home", open_it=True, opener=lambda url: "")
        (tmp_path / "proj").mkdir()
        (tmp_path / "home").mkdir()

        def work():
            app = ChatApp(cwd=tmp_path / "proj", home=tmp_path / "home", console=front.console, read_line=front.read,
                          read_short=front.read_short, select=front.select, env={},
                          model_factory=lambda name, env: MockModel(script=[make_final("ok")]))
            front.wire(app)
            try:
                return app.start()
            except EOFError:
                return 0

        def window():                                     # a window that connects, then is closed
            time.sleep(0.3)
            front.window_connected(True)
            time.sleep(0.2)
            front.window_connected(False)

        import threading
        threading.Thread(target=window, daemon=True).start()
        started = time.monotonic()
        assert front.run(work) == 0
        assert time.monotonic() - started < 15
