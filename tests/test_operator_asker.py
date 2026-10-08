"""The built-in asker (ask_user=True): notebook, terminal, controlling terminal, headless."""

import asyncio
import builtins
import io
import sys
import threading
import time
import types

import pytest

from agentx_dev import Operator as op


class ZMQInteractiveShell:
    def __init__(self, allow_stdin=True):
        self.kernel = types.SimpleNamespace(_allow_stdin=allow_stdin)


class ColabShell(ZMQInteractiveShell):
    """Colab's shell subclasses the Jupyter one under another name."""


class StdinNotImplementedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def fresh_reader_guard():
    op._release_reader()
    yield
    op._release_reader()


def wait_until(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fake_ipython(monkeypatch, shell):
    mod = types.ModuleType("IPython")
    mod.get_ipython = lambda: shell
    monkeypatch.setitem(sys.modules, "IPython", mod)


def no_ipython(monkeypatch):
    monkeypatch.delitem(sys.modules, "IPython", raising=False)


def tty_stdin(monkeypatch, is_tty=True):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: is_tty))


class TestNotebook:
    def test_a_notebook_uses_input(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "Notion")
        assert op.builtin_asker("Which competitors?") == "Notion"
        assert "Which competitors?" in seen[0]

    def test_colabs_subclassed_shell_counts_as_a_notebook(self, monkeypatch):
        fake_ipython(monkeypatch, ColabShell())
        monkeypatch.setattr(builtins, "input", lambda prompt="": "ok")
        assert op._in_notebook() is True
        assert op.builtin_asker("q") == "ok"

    def test_a_kernel_that_cannot_take_input_is_no_channel(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell(allow_stdin=False))
        monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("must not prompt"))
        with pytest.raises(op.NoChannel):
            op.builtin_asker("q")

    def test_stdin_not_implemented_is_no_channel(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())

        def refuse(prompt=""):
            raise StdinNotImplementedError("raw_input was called, but this frontend does not support input requests.")
        monkeypatch.setattr(builtins, "input", refuse)
        with pytest.raises(op.NoChannel):
            op.builtin_asker("q")

    def test_the_notebook_path_ignores_ask_timeout(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())
        monkeypatch.setattr(builtins, "input", lambda prompt="": "ok")
        monkeypatch.setattr(op, "_call_with_timeout", lambda *a, **k: pytest.fail("no thread in a notebook"))
        assert op.builtin_asker("q", timeout=1.0) == "ok"

    def test_a_plain_ipython_terminal_is_not_a_notebook(self, monkeypatch):
        class TerminalInteractiveShell:
            pass
        fake_ipython(monkeypatch, TerminalInteractiveShell())
        assert op._in_notebook() is False


class TestTerminal:
    def test_a_terminal_uses_input(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "Obsidian")
        assert op.builtin_asker("Which?", prefix="[sup]") == "Obsidian"
        assert "[sup]" in seen[0] and "Which?" in seen[0]

    def test_eof_means_no_answer(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)

        def eof(prompt=""):
            raise EOFError
        monkeypatch.setattr(builtins, "input", eof)
        assert op.builtin_asker("q") is None

    def test_keyboard_interrupt_stops_the_run(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)

        def interrupt(prompt=""):
            raise KeyboardInterrupt
        monkeypatch.setattr(builtins, "input", interrupt)
        with pytest.raises(KeyboardInterrupt):
            op.builtin_asker("q")

    def test_an_explicit_timeout_reads_in_a_thread(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = {}
        monkeypatch.setattr(op, "_call_with_timeout", lambda fn, timeout: seen.setdefault("timeout", timeout) and "x")
        assert op.builtin_asker("q", timeout=7.0) == "x"
        assert seen["timeout"] == 7.0


class TestControllingTerminal:
    def test_it_prompts_on_the_controlling_terminal_when_stdin_is_not_a_tty(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        written = []

        class Out(io.StringIO):
            def close(self):                       # the asker closes the handle; keep what it wrote
                written.append(self.getvalue())
                super().close()
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("Coda\n"), Out()))
        assert op.builtin_asker("Which?") == "Coda"
        assert "Which?" in written[0]

    def test_a_blank_line_is_an_empty_answer_and_eof_is_none(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("\n"), io.StringIO()))
        assert op.builtin_asker("q") == ""
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO(""), io.StringIO()))
        assert op.builtin_asker("q") is None

    def test_headless_is_no_channel(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (None, None))
        with pytest.raises(op.NoChannel):
            op.builtin_asker("q")

    def test_it_defaults_to_a_300_second_timeout_and_honours_an_explicit_one(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("a\n"), io.StringIO()))
        seen = []
        real = op._call_with_timeout
        monkeypatch.setattr(op, "_call_with_timeout", lambda fn, timeout: seen.append(timeout) or real(fn, timeout))
        op.builtin_asker("q")
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("a\n"), io.StringIO()))
        op.builtin_asker("q", timeout=12.0)
        assert seen == [300.0, 12.0] and op.CONTROLLING_TTY_TIMEOUT == 300.0

    def test_a_reader_that_never_returns_times_out(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        gate = threading.Event()

        class Stuck(io.StringIO):
            def readline(self, *a):
                gate.wait(5)
                return ""
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (Stuck(), io.StringIO()))
        try:
            with pytest.raises(op.AskTimeout):
                op.builtin_asker("q", timeout=0.05)
        finally:
            gate.set()


class TestCallWithTimeout:
    def test_it_returns_the_value(self):
        assert op._call_with_timeout(lambda: 42, 1.0) == 42

    def test_it_times_out(self):
        gate = threading.Event()
        try:
            with pytest.raises(op.AskTimeout):
                op._call_with_timeout(lambda: gate.wait(5), 0.05)
        finally:
            gate.set()

    def test_it_re_raises_what_the_function_raised(self):
        def boom():
            raise ValueError("nope")
        with pytest.raises(ValueError, match="nope"):
            op._call_with_timeout(boom, 1.0)


    def test_a_timeout_still_fires_in_about_the_requested_time(self):
        gate = threading.Event()
        started = time.monotonic()
        try:
            with pytest.raises(op.AskTimeout):
                op._call_with_timeout(lambda: gate.wait(5), 0.05)
            assert time.monotonic() - started < 1.0
        finally:
            gate.set()

    def test_a_slow_value_still_returns_and_it_waits_in_short_slices(self, monkeypatch):
        joins = []
        real_join = threading.Thread.join

        def spy(self, timeout=None):
            joins.append(timeout)
            return real_join(self, timeout)
        monkeypatch.setattr(threading.Thread, "join", spy)

        def slow():
            time.sleep(0.5)
            return "late but in time"
        assert op._call_with_timeout(slow, 5.0) == "late but in time"
        assert joins and max(joins) <= 0.2 + 1e-9             # never one long lock-wait

    def test_a_zero_timeout_times_out_at_once(self):
        gate = threading.Event()
        try:
            with pytest.raises(op.AskTimeout):
                op._call_with_timeout(lambda: gate.wait(5), 0)
        finally:
            gate.set()


class TestAbandonedReader:
    """A reader abandoned by a timeout is still blocked; a later ask must not queue behind it."""

    def test_controlling_terminal_refuses_while_an_abandoned_reader_is_waiting(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        gate, entered = threading.Event(), threading.Event()

        class Stuck(io.StringIO):
            def readline(self, *a):
                entered.set()
                gate.wait(5)
                return "late\n"
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (Stuck(), io.StringIO()))
        try:
            with pytest.raises(op.AskTimeout):
                op.builtin_asker("first", timeout=0.05)
            assert entered.wait(3)

            monkeypatch.setattr(op, "_open_controlling_tty", lambda: pytest.fail("must not open a second handle"))
            with pytest.raises(op.NoChannel, match="still waiting"):
                op.builtin_asker("second", timeout=0.05)
        finally:
            gate.set()
        assert wait_until(lambda: not op._reader_busy)

        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("fresh\n"), io.StringIO()))
        assert op.builtin_asker("third") == "fresh"

    def test_terminal_refuses_while_an_abandoned_reader_is_waiting(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def blocking_input(prompt=""):
            calls.append(prompt)
            entered.set()
            gate.wait(5)
            return "late"
        monkeypatch.setattr(builtins, "input", blocking_input)
        try:
            with pytest.raises(op.AskTimeout):
                op.builtin_asker("first", timeout=0.05)
            assert entered.wait(3)

            with pytest.raises(op.NoChannel, match="still waiting"):
                op.builtin_asker("second", timeout=0.05)
            assert len(calls) == 1                       # the second ask never reached input()
        finally:
            gate.set()
        assert wait_until(lambda: not op._reader_busy)

        monkeypatch.setattr(builtins, "input", lambda prompt="": "fresh")
        assert op.builtin_asker("third", timeout=1.0) == "fresh"

    def test_a_finished_read_leaves_the_guard_clear(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("a\n"), io.StringIO()))
        assert op.builtin_asker("q") == "a"
        assert op._reader_busy is False

        tty_stdin(monkeypatch)
        monkeypatch.setattr(builtins, "input", lambda prompt="": "b")
        assert op.builtin_asker("q", timeout=1.0) == "b"
        assert op._reader_busy is False

        def eof(prompt=""):
            raise EOFError
        monkeypatch.setattr(builtins, "input", eof)
        assert op.builtin_asker("q", timeout=1.0) is None
        assert op._reader_busy is False


class TestPromptText:
    def test_control_characters_in_a_model_written_question_never_reach_the_terminal(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "ok")
        op.builtin_asker("Which?\x1b[2J\x1b]0;pwned\x07 \rline\x00\x7f\tafter\nnext", prefix="[a]")
        shown = seen[0]
        assert not any(ord(c) < 32 and c not in "\n\t" for c in shown) and "\x7f" not in shown
        assert "Which?" in shown and "\tafter\n  next" in shown       # newline kept; continuation lines are indented

    def test_the_controlling_terminal_prompt_is_cleaned_too(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        written = []

        class Out(io.StringIO):
            def close(self):
                written.append(self.getvalue())
                super().close()
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("a\n"), Out()))
        op.builtin_asker("Q\x1b[31m red\x07")
        assert "\x1b" not in written[0] and "\x07" not in written[0] and "Q" in written[0]

    def test_the_notebook_prompt_is_cleaned_too(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "ok")
        op.builtin_asker("Q\x1b[31m red")
        assert "\x1b" not in seen[0]

    def test_ordinary_text_is_unchanged(self):
        text = "Which three competitors?\n(context: step 1)\tok"
        assert op._strip_controls(text) == text


class TestAsyncTerminalRead:
    """The untimed async terminal read runs in a daemon thread and holds the reader guard."""

    def test_the_read_runs_in_a_daemon_thread_off_the_loop(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = {}

        def typed(prompt=""):
            seen["thread"] = threading.current_thread()
            return "Obsidian"
        monkeypatch.setattr(builtins, "input", typed)

        async def go():
            return await op.OperatorChannel.create(True, is_async=True).aask("Which?")
        reply = asyncio.run(go())
        assert reply.text == "Obsidian"
        assert seen["thread"].daemon and seen["thread"] is not threading.main_thread()
        assert op._reader_busy is False

    def test_a_reader_exception_is_mapped_and_the_guard_is_released(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)

        def boom(prompt=""):
            raise RuntimeError("terminal on fire")
        monkeypatch.setattr(builtins, "input", boom)
        reply = asyncio.run(op.OperatorChannel.create(True, is_async=True).aask("q"))
        assert reply.reason == "error" and op._reader_busy is False

    def test_keyboard_interrupt_from_the_reader_propagates(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)

        def interrupt(prompt=""):
            raise KeyboardInterrupt
        monkeypatch.setattr(builtins, "input", interrupt)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(op.OperatorChannel.create(True, is_async=True).aask("q"))
        assert op._reader_busy is False

    def test_cancelling_the_ask_leaves_a_guarded_daemon_reader(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        gate, entered = threading.Event(), threading.Event()
        readers = []

        def blocking_input(prompt=""):
            readers.append(threading.current_thread())
            entered.set()
            gate.wait(5)
            return "late"
        monkeypatch.setattr(builtins, "input", blocking_input)
        out = {}

        async def go():
            ch = op.OperatorChannel.create(True, is_async=True)
            task = asyncio.create_task(ch.aask("first question", source="a"))
            for _ in range(300):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            out["cancel_seconds"] = time.monotonic() - started
            out["second"] = await ch.aask("second question", source="b")
        try:
            asyncio.run(go())
        finally:
            gate.set()
        assert out["cancel_seconds"] < 1.0                         # (a) prompt cancellation
        assert out["second"].reason == "no_channel"                # (b) refused, no stolen answer
        assert len(readers) == 1                                   #     the second ask never read
        assert wait_until(lambda: not op._reader_busy)             # (c) the guard clears once released
        assert readers[0].daemon                                   # (d) the orphan cannot hold the process

        monkeypatch.setattr(builtins, "input", lambda prompt="": "fresh")
        reply = asyncio.run(op.OperatorChannel.create(True, is_async=True).aask("third"))
        assert reply.text == "fresh"


class TestPromptShowsWhoIsAsking:
    """Every built-in prompt starts with `[who] <hand> needs your input`, so a GUI shows who asked."""

    HAND = "\U0001F64B"

    def test_the_banner_has_the_prefix_the_hand_and_the_question(self):
        text = op._banner("[planner]", "Which competitors?")
        assert text == f"\n[planner] {self.HAND} needs your input\n  question: Which competitors?\n"

    def test_a_context_line_is_kept_on_its_own_line(self):
        text = op._banner("[helper]", "Which file?\n(context: step 2)")
        assert "  question: Which file?\n  (context: step 2)\n" in text

    def test_the_plain_banner_has_no_hand(self):
        assert self.HAND not in op._banner("[x]", "q", emoji=False)
        assert "[x] needs your input" in op._banner("[x]", "q", emoji=False)

    def test_a_notebook_prompt_names_who_asked(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "ok")
        op.builtin_asker("Which competitors?", prefix="[pricing_researcher]")
        assert seen[0].startswith(f"\n[pricing_researcher] {self.HAND} needs your input")
        assert "question: Which competitors?" in seen[0] and seen[0].endswith("> ")

    def test_a_terminal_prompt_names_who_asked(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "ok")
        op.builtin_asker("Which?", prefix="[planner]")
        assert f"[planner] {self.HAND} needs your input" in seen[0]

    def test_the_controlling_terminal_banner_names_who_asked(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        written = []

        class Out(io.StringIO):
            def close(self):
                written.append(self.getvalue())
                super().close()
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("Coda\n"), Out()))
        assert op.builtin_asker("Which?", prefix="[planner]") == "Coda"
        assert f"[planner] {self.HAND} needs your input" in written[0]

    def test_a_console_that_cannot_print_the_hand_gets_the_plain_banner(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch)
        seen = []

        def input_ascii_only(prompt=""):
            seen.append(prompt)
            prompt.encode("ascii")                         # what a cp1252-style console would do
            return "ok"
        monkeypatch.setattr(builtins, "input", input_ascii_only)
        assert op.builtin_asker("Which?", prefix="[planner]") == "ok"
        assert len(seen) == 2 and self.HAND in seen[0] and self.HAND not in seen[1]
        assert "[planner] needs your input" in seen[1]

    def test_a_controlling_terminal_that_cannot_print_the_hand_gets_the_plain_banner(self, monkeypatch):
        no_ipython(monkeypatch)
        tty_stdin(monkeypatch, is_tty=False)
        raw = io.BytesIO()
        ascii_out = io.TextIOWrapper(raw, encoding="ascii", write_through=True)
        captured = []
        real_close = ascii_out.close

        def close():
            captured.append(raw.getvalue().decode("ascii"))
            real_close()
        ascii_out.close = close
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("Coda\n"), ascii_out))
        assert op.builtin_asker("Which?", prefix="[planner]") == "Coda"
        assert "[planner] needs your input" in captured[0] and self.HAND not in captured[0]

    def test_control_characters_are_still_stripped_from_the_question(self, monkeypatch):
        fake_ipython(monkeypatch, ZMQInteractiveShell())
        seen = []
        monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "ok")
        op.builtin_asker("Which\x1b[2J file?", prefix="[x]")
        assert "\x1b" not in seen[0] and "Which[2J file?" in seen[0]
