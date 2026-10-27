"""Full-screen mode: the header and the input stay put while the conversation scrolls. These tests run the real
screen with keys sent through a pipe and a dummy output, so nothing is drawn."""

import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from agentx_dev.cli.fullscreen import FullScreen
from agentx_dev.cli.settings import read_user_fullscreen, write_user_fullscreen
from agentx_dev.cli.ui import CANCELLED, Progress, Screen
from tests.conftest import MockModel, make_final


@pytest.fixture
def keys():
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            yield pipe


def make(tmp_path, cycle=lambda: None):
    return FullScreen(history_file=tmp_path / "history", commands=["/help", "/exit"], toolbar=lambda: "status",
                      cycle_mode=cycle)


def type_in(screen, keys, steps, timeout=10.0):
    """For each (kind, text): wait until the screen asks a new question of that kind, then type the text."""
    def run():
        last = None
        for kind, text in steps:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                request = screen._request
                if request is not None and request is not last and request["kind"] == kind:
                    last = request
                    keys.send_text(text)
                    break
                time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def row_text(row):
    return "".join(text for _, text in row)


class TestTheConversation:
    def test_what_is_printed_goes_to_the_conversation_and_it_scrolls(self, tmp_path, keys):
        screen = make(tmp_path)
        for number in range(1, 51):
            screen.console.print(f"line {number}")
        rows = screen.visible(10)
        assert [row_text(row) for row in rows][-1].startswith("line 50") and len(rows) == 10
        screen.scroll_by(-5)
        assert row_text(screen.visible(10)[0]).startswith("line 36") and screen.scrolled_up()
        screen.scroll_by(100)
        assert not screen.scrolled_up() and row_text(screen.visible(10)[-1]).startswith("line 50")

    def test_the_header_is_the_welcome_box_or_one_line_on_a_short_window(self, tmp_path, keys):
        screen = make(tmp_path)
        assert screen.header_height() == 0
        from rich.panel import Panel
        screen.set_header(lambda width: Panel("WELCOME", width=width), "agentx v1 · model · folder")
        text = "".join(t for _, t in screen.header_fragments())
        assert "WELCOME" in text and screen.header_height() == 3            # DummyOutput is 40 rows high
        screen._rows = lambda: 20
        screen._header_cache = (None, [])
        assert "".join(t for _, t in screen.header_fragments()).strip() == "agentx v1 · model · folder"


class TestQuestions:
    def test_a_message_a_choice_and_an_answer(self, tmp_path, keys, capsys):
        screen = make(tmp_path)
        answers = {}

        def work():
            type_in(screen, keys, [("chat", "hello there\r"), ("select", "2"), ("short", "maybe\r")])
            answers["message"] = screen.read()
            answers["choice"] = screen.select(["Yes", "No"], hotkeys={"n": 1})
            answers["short"] = screen.read_short("Sure? ")
            screen.console.print("all done")
            return 7

        assert screen.run(work) == 7
        assert answers == {"message": "hello there", "choice": 1, "short": "maybe"}
        shown = capsys.readouterr().out                                    # the conversation, written on exit
        assert "❯ hello there" in shown and "> 2. No" in shown and "Sure? maybe" in shown and "all done" in shown

    def test_a_hotkey_picks_and_ctrl_c_cancels_a_menu(self, tmp_path, keys):
        screen = make(tmp_path)
        answers = []

        def work():
            type_in(screen, keys, [("select", "n"), ("select", "\x03")])
            answers.append(screen.select(["Yes", "No"], hotkeys={"n": 1}))
            answers.append(screen.select(["Yes", "No"]))
            return 0

        screen.run(work)
        assert answers == [1, CANCELLED]

    def test_ctrl_c_and_ctrl_d_at_the_input(self, tmp_path, keys):
        screen = make(tmp_path)
        seen = []

        def work():
            type_in(screen, keys, [("chat", "half typed\x03"), ("chat", "\x04")])
            for _ in range(2):
                try:
                    screen.read()
                except (KeyboardInterrupt, EOFError) as error:
                    seen.append(type(error).__name__)
            return 0

        screen.run(work)
        assert seen == ["KeyboardInterrupt", "EOFError"]

    def test_ctrl_c_while_the_agent_works_interrupts_it(self, tmp_path, keys):
        screen = make(tmp_path)

        def work():
            threading.Thread(target=lambda: (time.sleep(0.4), keys.send_text("\x03")), daemon=True).start()
            try:
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    time.sleep(0.01)
            except KeyboardInterrupt:
                return 5
            return 1

        assert screen.run(work) == 5

    def test_a_message_typed_while_the_agent_works_is_kept_for_later(self, tmp_path, keys):
        screen = make(tmp_path)

        def work():
            keys.send_text("next thing\r")
            end = time.monotonic() + 10
            while screen._queued is None and time.monotonic() < end:
                time.sleep(0.02)
            return 0 if screen.read() == "next thing" else 1

        assert screen.run(work) == 0

    def test_shift_tab_cycles_the_mode(self, tmp_path, keys):
        cycled = []
        screen = make(tmp_path, cycle=lambda: cycled.append(1))

        def work():
            type_in(screen, keys, [("chat", "\x1b[Z\x04")])         # Shift+Tab, then Ctrl+D to leave
            try:
                screen.read()
            except EOFError:
                pass
            return 0

        screen.run(work)
        assert cycled == [1]


class TestTheAppInFullScreen:
    def test_the_spinner_is_drawn_by_the_screen_not_by_rich(self):
        progress = Progress(Console(force_terminal=True))
        redraws = []
        progress.drawn_by = lambda: redraws.append(1)
        progress.start("Thinking")
        assert progress.renderable() is not None and progress._live is None and redraws
        progress.stop()
        assert progress.renderable() is None

    def test_the_welcome_box_becomes_the_header(self):
        console = Console(force_terminal=False)
        screen = Screen(console)
        headers = []
        screen.header = lambda factory, compact: headers.append((factory(80), compact))
        screen.welcome(version="3.7.0", greeting="Hi", model="gpt · auto mode", folder="~/proj", tips=[], news=[])
        assert headers and headers[0][1] == "agentx v3.7.0 · gpt · auto mode · ~/proj"

    def test_a_whole_turn(self, tmp_path, keys, capsys):
        from agentx_dev.cli.app import ChatApp
        screen = make(tmp_path)
        cwd, home = tmp_path / "proj", tmp_path / "home"
        cwd.mkdir()
        home.mkdir()
        model = MockModel(script=[make_final("Hi from the model")])
        app = ChatApp(cwd=cwd, home=home, console=screen.console, read_line=screen.read,
                      read_short=screen.read_short, select=screen.select, mode="auto", env={},
                      model_factory=lambda name, env: model)
        app.fullscreen = True
        app.screen.header = screen.set_header
        app.progress.drawn_by = screen.invalidate
        screen.progress = app.progress
        type_in(screen, keys, [("chat", "hello\r"), ("chat", "\x04")])
        assert screen.run(app.start) == 0
        shown = capsys.readouterr().out
        assert "❯ hello" in shown and "Hi from the model" in shown
        assert screen._header is not None and "Tips for getting started" not in shown     # the box is the header


def test_the_setting_is_the_users_own(tmp_path):
    assert read_user_fullscreen(tmp_path) is False
    write_user_fullscreen(tmp_path, True)
    assert read_user_fullscreen(tmp_path) is True
    write_user_fullscreen(tmp_path, False)
    assert read_user_fullscreen(tmp_path) is False
    (tmp_path / ".agentx" / "settings.json").write_text("not json", encoding="utf-8")
    assert read_user_fullscreen(tmp_path) is False


def test_the_flag_wins_over_the_setting():
    from agentx_dev.cli.app import build_parser
    assert build_parser().parse_args([]).fullscreen is None
    assert build_parser().parse_args(["--fullscreen"]).fullscreen is True
    assert build_parser().parse_args(["--no-fullscreen"]).fullscreen is False
