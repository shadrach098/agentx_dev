"""Commands typed while the agent works: the ones that only show something (and /mode) run at once, /voice starts
recording at once, /unsend takes back a waiting message; the rest still wait until the agent is done.

Before, every command typed under the spinner waited (`/voice · runs when the agent is done`), and a message once
sent could not be taken back."""

import io
import time

import pytest
from rich.console import Console

from agentx_dev.cli.commands import runs_while_working
from agentx_dev.cli.team import _Job
from agentx_dev.cli.typeahead import TypeAhead
from agentx_dev.cli.ui import Progress
from agentx_dev.cli.voice import SAMPLE_RATE, to_wav
from tests.conftest import MockModel, make_final
from tests.test_cli_app import Lines, all_seen_by, make_app
from tests.test_cli_voice import FakeRecorder, PrefillLines
from tests.test_typing_while_working import FakeKeys


@pytest.mark.parametrize("line", [
    "/help", "/usage", "/settings", "/sessions", "/memory", "/memory login redirect", "/mode", "/mode plan",
    "/model", "/team-models", "/limit", "/fullscreen", "/skills", "/mcp", "/mcp tools github", "/team log",
])
def test_these_run_at_once(line):
    assert runs_while_working(line)


@pytest.mark.parametrize("line", [
    "/model gpt-5.5", "/clear", "/resume", "/init", "/team fix it all", "/exit", "/skills install https://x/y",
    "/skills remove a", "/mcp add a https://x", "/mcp login a", "/limit 300k", "/team-models gpt-5.5",
    "/fullscreen on", "/team-models clear", "fix the footer", "/tmp/x.log fails",
])
def test_these_still_wait(line):
    assert not runs_while_working(line)


def wait_until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class TestRunsNow:
    def test_a_show_command_runs_during_the_turn(self, tmp_path):
        seen = {}

        def script(messages):
            app.typed_while_working("/settings")
            seen["waiting"] = app._waiting_shown()
            seen["printed"] = out.getvalue()
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        assert seen["waiting"] == []
        printed = seen["printed"]
        assert "/settings  ✓ ran now" in printed
        assert len(printed.split("/settings  ✓ ran now", 1)[1].strip()) > 20     # the settings were shown, mid-turn
        assert "running it now" not in out.getvalue()

    def test_mode_changes_at_once(self, tmp_path):
        seen = {}

        def script(messages):
            app.typed_while_working("/mode plan")
            seen["mode"] = app.gate.mode
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        assert seen["mode"] == "plan"

    def test_a_command_that_changes_things_still_waits(self, tmp_path):
        seen = {}

        def script(messages):
            app.typed_while_working("/team-models clear")
            seen["waiting"] = app._waiting_shown()
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        assert seen["waiting"] == [("/team-models clear", "runs when the agent is done")]


class TestUnsend:
    def test_the_newest_is_taken_back(self, tmp_path):
        seen = {}

        def script(messages):
            if "seen" in seen:
                return make_final("done")
            seen["seen"] = True
            app.typed_while_working("keep the footer")
            app.typed_while_working("delete the whole site")
            app.typed_while_working("/unsend")
            seen["waiting"] = app._waiting_shown()
            return '{"Thought": "x", "action": "noop", "action_input": "x"}'
        model = MockModel(script=script)
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), model)
        app.start()
        assert [text for text, _ in seen["waiting"]] == ["keep the footer"]
        assert "/unsend  ✓ unsent: delete the whole site" in out.getvalue()
        assert "delete the whole site" not in all_seen_by(model)

    def test_by_number_and_all(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        for text in ("one", "two", "three"):
            app.typed_while_working(text)
        app.typed_while_working("/unsend 2")
        assert [text for text, _ in app._waiting_shown()] == ["one", "three"]
        app.typed_while_working("/unsend all")
        assert app._waiting_shown() == [] and app._take_typed() == []
        assert "unsent 2 messages" in out.getvalue()

    def test_nothing_waiting_and_bad_numbers(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        app.typed_while_working("/unsend")
        app.typed_while_working("hello")
        app.typed_while_working("/unsend 5")
        app.typed_while_working("/unsend soon")
        text = out.getvalue()
        assert "nothing is waiting" in text and "there is no message 5: 1 waiting" in text
        assert "use /unsend, /unsend <number> or /unsend all" in text

    def test_a_message_already_read_cannot_be_taken_back(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        app.typed_while_working("hello")
        assert app._take_typed() == ["hello"]               # the agent read it at its step
        app.typed_while_working("/unsend")
        assert "already read" in out.getvalue()

    def test_a_queued_command_can_be_taken_back(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        app.typed_while_working("/clear")
        app.typed_while_working("/unsend")
        assert app._next_typed() is None

    def test_the_lines_are_numbered(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120, color_system=None)
        progress = Progress(console)
        progress.waiting = lambda: [("one", "waiting for the agent's next step"), ("two", "runs when the agent is done")]
        progress.start("Thinking")
        try:
            with console.capture() as shown:
                console.print(progress)
            assert "◌ 1. one" in shown.get() and "◌ 2. two" in shown.get()
        finally:
            progress.stop()


class TestTeamNotes:
    def test_a_note_nobody_read_is_withdrawn(self):
        job = _Job()
        first = job.add_note("add a wishlist")
        job.add_note("use the brand colours")
        assert job.withdraw(first) is True
        assert job.kept_notes() == ["use the brand colours"]
        assert job.withdraw(first) is False                 # once

    def test_a_note_a_helper_read_stays(self, monkeypatch):
        from agentx_dev.cli import team
        job = _Job()
        index = job.add_note("add a wishlist")

        class Status:
            notes_seen = 0
            helper = "explorer"
        token = team._running_status.set(Status())
        try:
            assert job.new_notes() == ["add a wishlist"]
        finally:
            team._running_status.reset(token)
        assert job.withdraw(index) is False
        assert job.kept_notes() == ["add a wishlist"]

    def test_a_withdrawn_note_is_not_handed_to_a_helper(self):
        from agentx_dev.cli import team
        job = _Job()
        job.withdraw(job.add_note("nope"))
        job.add_note("yes")

        class Status:
            notes_seen = 0
            helper = "coder"
        token = team._running_status.set(Status())
        try:
            assert job.new_notes() == ["yes"]
        finally:
            team._running_status.reset(token)


def voice_app(tmp_path, lines, words="add a dark mode toggle", recorder=None):
    app, out, cwd, home = make_app(tmp_path, lines, MockModel(script=[make_final("done")]),
                                   env={"OPENAI_API_KEY": "sk-test"})
    app._recorder_factory = lambda: recorder or FakeRecorder()
    app._transcriber = lambda wav, key, model=None: words
    return app, out


class TestVoiceWhileItWorks:
    def test_it_starts_at_once_and_enter_stops_it(self, tmp_path):
        seen = {}
        lines = PrefillLines("hello", None, "/exit")

        def script(messages):
            app.typed_while_working("/voice")
            seen["recording"] = app._waiting_shown()
            app.typed_while_working("")                     # Enter on the empty line under the spinner
            wait_until(lambda: app._waiting_shown() == [])
            return make_final("done")
        app, out = voice_app(tmp_path, lines)
        app.model_factory = lambda name, env: MockModel(script=script)
        app.start()
        assert seen["recording"] == [("/voice", "● recording… press Enter to stop")]
        assert "add a dark mode toggle" in lines.defaults      # the next input line starts with the words
        assert "the words are in the input line" in out.getvalue()

    def test_the_words_go_into_the_line_being_typed(self, tmp_path):
        app, out = voice_app(tmp_path, Lines())
        typeahead = TypeAhead(lambda text: None, keys=FakeKeys())
        typeahead.resume()
        app.progress.typeahead = typeahead
        typeahead.feed("please ")
        app.typed_while_working("/voice")
        assert typeahead.submit_empty is True
        app.typed_while_working("")
        assert wait_until(lambda: typeahead.line() == "please add a dark mode toggle")
        assert typeahead.submit_empty is False
        typeahead.close()

    def test_unsend_cancels_the_recording(self, tmp_path):
        recorder = FakeRecorder()
        app, out = voice_app(tmp_path, Lines(), recorder=recorder)
        app.typed_while_working("/voice")
        app.typed_while_working("/unsend")
        assert recorder.cancelled and not app.recording() and app._waiting_shown() == []

    def test_enter_at_the_prompt_stops_a_recording_left_running(self, tmp_path):
        lines = PrefillLines("", None, "/exit")
        app, out = voice_app(tmp_path, lines)
        app.typed_while_working("/voice")                   # started during an earlier turn
        app.start()
        assert "Still recording" in out.getvalue()
        assert wait_until(lambda: not app._waiting_shown())

    def test_no_key(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), env={})
        app._saved_key_reader = lambda: {}
        app.typed_while_working("/voice")
        assert "OPENAI_API_KEY" in out.getvalue() and not app.recording()


class TestTypeAheadForVoice:
    def test_empty_enter_is_handed_over_only_while_recording(self):
        sent = []
        typed = TypeAhead(sent.append, keys=FakeKeys())
        typed.feed("\r")
        assert sent == []
        typed.submit_empty = True
        typed.feed("\r")
        assert sent == [""]

    def test_insert(self):
        typed = TypeAhead(lambda text: None, keys=FakeKeys())
        typed.feed("hi")
        typed.insert(" there ")
        assert typed.line() == "hi there"


def test_words_go_into_an_open_prompt():
    """After the turn, the /voice words land in the prompt that is already open (normal screen)."""
    import threading
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agentx_dev.cli.ui import PromptInput
    with create_pipe_input() as pipe:
        prompt = PromptInput.__new__(PromptInput)
        prompt._chat = PromptSession(input=pipe, output=DummyOutput())
        prompt._short = None
        assert prompt.insert("too early") is False            # no prompt open yet
        result = {}
        reader = threading.Thread(target=lambda: result.setdefault("line", prompt._chat.prompt("> ")))
        reader.start()
        assert wait_until(lambda: prompt.insert("add a dark mode toggle"))
        time.sleep(0.2)
        pipe.send_text("\r")
        reader.join(5)
        assert result.get("line") == "add a dark mode toggle"
