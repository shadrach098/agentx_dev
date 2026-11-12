"""Keys typed for the chat never answer a question: an approval menu or the folder trust question.

The menus are the real prompt_toolkit ones, driven by keys written to a pipe with a dummy output, so no terminal is
needed. ``ui.MENU_ARM_SEC`` (keys pressed this soon after a menu opens are ignored) is set per test."""

import io
from pathlib import Path
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from agentx_dev.cli import app as app_module
from agentx_dev.cli import trust as trust_module
from agentx_dev.cli import ui
from agentx_dev.cli.approval import Request
from agentx_dev.cli.fullscreen import FullScreen
from agentx_dev.cli.trust import TRUST_OPTIONS, ensure_trusted, is_trusted
from agentx_dev.cli.typeahead import TypeAhead
from agentx_dev.cli.ui import CANCELLED, make_asker, select_menu
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, make_app

GRANT_LABELS = ["Yes", "Yes, and don't ask again: all edits this session", "No"]


@pytest.fixture
def pipe(monkeypatch):
    monkeypatch.setattr(ui, "is_terminal", lambda: True)
    with create_pipe_input() as keys:
        with create_app_session(input=keys, output=DummyOutput()):
            yield keys


def send_later(keys, text, after):
    timer = threading.Timer(after, lambda: keys.send_text(text))
    timer.daemon = True
    timer.start()
    return timer


def when(condition, then, timeout=10.0):
    """On a thread: wait until ``condition()`` holds, then call ``then()``."""
    def run():
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                then()
                return
            time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def edit_request():
    return Request(tool="write_file", kind="edit", subject="hello.txt", args={"path": "hello.txt", "content": "hi"},
                   remember=True, grant="all edits this session")


# -- the normal screen's menu ----------------------------------------------------------------------------------

class TestSelectMenu:
    def test_keys_typed_before_the_menu_do_not_answer_it(self, pipe, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0.3)
        pipe.send_text("please fix the tests\r")           # typed for the chat; Enter would have been Yes
        send_later(pipe, "3", 0.6)                          # a deliberate answer after the menu is up
        assert select_menu(GRANT_LABELS, 0, {"y": 0, "a": 1, "n": 2}) == 2

    def test_keys_waiting_in_the_terminal_are_thrown_away_first(self, pipe, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        calls = []
        monkeypatch.setattr(ui, "discard_typed_ahead", lambda: calls.append(1))
        pipe.send_text("2")
        assert select_menu(GRANT_LABELS, 0) == 1
        assert calls == [1]

    def test_ctrl_c_cancels_even_at_once(self, pipe, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 30)
        pipe.send_text("\x03")
        assert select_menu(GRANT_LABELS, 0) == CANCELLED

    def test_discarding_without_a_terminal_does_nothing_and_never_raises(self):
        ui.discard_typed_ahead()                            # pytest's stdin is not a terminal


class TestApprovalMenu:
    def asker(self, select, replies=("",)):
        replies = list(replies)
        return make_asker(Console(file=io.StringIO(), width=100, color_system=None),
                          lambda prompt="": replies.pop(0) if replies else "", Path("."), select=select)

    def test_the_letter_a_is_not_a_shortcut_for_dont_ask_again(self, pipe, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        seen = []

        def select(options, default=0, hotkeys=None):
            seen.append(dict(hotkeys or {}))
            return select_menu(options, default, hotkeys)

        pipe.send_text("an")                                # 'a' does nothing, 'n' declines
        answer = self.asker(select)(edit_request())
        assert answer.choice == "n"
        assert "a" not in seen[0] and seen[0] == {"y": 0, "n": 2}

    def test_dont_ask_again_is_still_its_number_or_the_arrows(self, pipe, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        pipe.send_text("2")
        assert self.asker(select_menu)(edit_request()).choice == "a"
        pipe.send_text("\x1b[B\r")
        assert self.asker(select_menu)(edit_request()).choice == "a"


def test_an_edit_approval_is_not_answered_by_a_message_typed_for_the_chat(tmp_path, pipe, monkeypatch):
    """End to end: the model asks to write a file while the user is still typing 'please ...' and Enter."""
    monkeypatch.setattr(ui, "MENU_ARM_SEC", 0.3)
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)
    model = MockModel(script=[make_react_response("write_file", {"path": "hello.txt", "content": "hi\n",
                                                                 "if_exists": "refuse"}),
                              make_final("Understood")])

    def select(options, default=0, hotkeys=None):
        pipe.send_text("please go on\r")                    # queued for the chat as the menu opens
        send_later(pipe, "3", 0.6)                          # then the user reads it and says No
        return select_menu(options, default, hotkeys)

    app, _, cwd, _ = make_app(tmp_path, Lines("create hello.txt", "", "/exit"), model, select=select)
    app.start()
    assert not (cwd / "hello.txt").exists()
    assert app.gate.mode != "accept-edits"


def test_approvals_go_through_the_type_ahead_hold(tmp_path, monkeypatch):
    """make_asker used to keep the select it was built with, so the hold _start_typeahead adds never applied."""
    class Keys:
        def enter(self):
            pass

        def leave(self):
            pass

        def read(self):
            return ""

    typed = {}

    def make_typeahead(on_submit):
        typed["it"] = TypeAhead(on_submit, keys=Keys())
        return typed["it"]

    holds = []

    def select(options, default=0, hotkeys=None):
        holds.append(typed["it"]._holds)
        return 0

    monkeypatch.setattr(app_module, "TypeAhead", make_typeahead)
    monkeypatch.setattr(ui.Progress, "enabled", property(lambda self: True))
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), select=select)
    app._start_typeahead()
    assert app._ask_remembering(edit_request()).choice == "y"
    assert holds == [1]


# -- the type-ahead reader -------------------------------------------------------------------------------------

class QueuedKeys:
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def enter(self):
        pass

    def leave(self):
        pass

    def read(self):
        return self.chunks.pop(0) if self.chunks else ""


def test_keys_still_waiting_when_the_reader_pauses_stay_with_the_chat():
    sent = []
    keys = QueuedKeys("fix the", " tests")
    typed = TypeAhead(sent.append, keys=keys)
    with typed._lock:                                   # resumed, but the reader thread has not read yet
        typed._active = True
    typed.pause()                                       # one last read: the chat gets it, not the next prompt
    assert typed.line() == "fix the" and sent == []
    assert keys.chunks == [" tests"]                    # anything left is for the menu to throw away


def test_an_enter_still_waiting_when_the_reader_pauses_sends_the_chat_line():
    sent = []
    keys = QueuedKeys("hi there\r")
    typed = TypeAhead(sent.append, keys=keys)
    with typed._lock:                                   # the reader thread cannot read first
        typed._active = True
    typed.pause()
    assert sent == ["hi there"] and typed.line() == ""


# -- full-screen mode -------------------------------------------------------------------------------------------

@pytest.fixture
def keys():
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yield pipe_input


def make_screen(tmp_path):
    return FullScreen(history_file=tmp_path / "history", commands=["/help", "/exit"], toolbar=lambda: "status",
                      cycle_mode=lambda: None)


def selecting(screen):
    return lambda: screen._request is not None and screen._request["kind"] == "select"


class TestFullScreenMenu:
    def test_enter_ending_a_message_sends_it_and_does_not_answer(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        screen = make_screen(tmp_path)
        out = {}

        def work():
            keys.send_text("fix the")                       # typed while the agent works
            end = time.monotonic() + 10
            while screen.chat_buffer.text != "fix the" and time.monotonic() < end:
                time.sleep(0.02)
            when(selecting(screen), lambda: keys.send_text(" tests\r"))
            when(lambda: screen._queued is not None, lambda: keys.send_text("n"))
            out["picked"] = screen.select(GRANT_LABELS, 0, {"y": 0, "n": 2})
            out["queued"] = screen._queued
            return 0

        screen.run(work)
        assert out == {"picked": 2, "queued": "fix the tests"}

    def test_a_word_typed_into_an_open_menu_goes_to_the_input_line(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        screen = make_screen(tmp_path)
        out = {}

        def work():
            when(selecting(screen), lambda: keys.send_text("please"))
            when(lambda: screen.chat_buffer.text == "please", lambda: keys.send_text("\x03"))
            out["picked"] = screen.select(GRANT_LABELS, 0, {"y": 0, "a": 1, "n": 2})   # even with an 'a' key
            out["line"] = screen.chat_buffer.text
            return 0

        screen.run(work)
        assert out == {"picked": CANCELLED, "line": "please"}

    def test_the_menu_shows_how_to_answer_while_a_message_is_written(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        screen = make_screen(tmp_path)
        out = {}

        def work():
            def look():
                out["lines"] = screen.live_lines()
                keys.send_text("\x03")
            when(selecting(screen), lambda: keys.send_text("hello"))
            when(lambda: screen.chat_buffer.text == "hello", look)
            screen.select(GRANT_LABELS, 0)
            return 0

        screen.run(work)
        assert any("Clear or send your message to answer" in line for line in out["lines"])

    def test_keys_in_the_first_moment_after_the_menu_opens_are_ignored(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0.3)
        screen = make_screen(tmp_path)
        out = {}

        def work():
            when(selecting(screen), lambda: (keys.send_text("y\r"), send_later(keys, "3", 0.6)))
            out["picked"] = screen.select(GRANT_LABELS, 0, {"y": 0, "n": 2})
            return 0

        screen.run(work)
        assert out["picked"] == 2


# -- the folder trust question ------------------------------------------------------------------------------------

def quiet():
    return Console(file=io.StringIO(), width=100, color_system=None)


class TestTrust:
    def test_every_front_end_is_asked_with_no_highlighted(self, tmp_path):
        seen = []
        proj, home = tmp_path / "proj", tmp_path / "home"
        proj.mkdir()
        ensure_trusted(proj, home, quiet(), select=lambda options, default=0, hotkeys=None:
                       (seen.append((list(options), default)), CANCELLED)[1])
        assert seen == [(list(TRUST_OPTIONS), 1)] and TRUST_OPTIONS[1] == "No, exit"

    @pytest.mark.parametrize("typed, trusted", [("\r", False), ("y", True), ("1", True), ("\x1b[A\r", True),
                                                ("n", False)])
    def test_enter_alone_declines_in_the_terminal_menu(self, tmp_path, pipe, monkeypatch, typed, trusted):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        proj, home = tmp_path / "proj", tmp_path / "home"
        proj.mkdir()
        pipe.send_text(typed)
        assert ensure_trusted(proj, home, quiet(), select=select_menu) is trusted
        assert is_trusted(proj, home) is trusted

    def test_waiting_keys_are_thrown_away_before_the_question(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(trust_module, "discard_typed_ahead", lambda: calls.append("discard"))
        proj, home = tmp_path / "proj", tmp_path / "home"
        proj.mkdir()
        ensure_trusted(proj, home, quiet(), select=lambda *args: (calls.append("ask"), 1)[1])
        assert calls == ["discard", "ask"]

    def test_enter_alone_declines_in_full_screen(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(ui, "MENU_ARM_SEC", 0)
        screen = make_screen(tmp_path)
        proj, home = tmp_path / "proj", tmp_path / "home"
        proj.mkdir()
        out = {}

        def work():
            when(selecting(screen), lambda: keys.send_text("\r"))
            out["trusted"] = ensure_trusted(proj, home, screen.console, select=screen.select)
            return 0

        screen.run(work)
        assert out["trusted"] is False and not is_trusted(proj, home)
