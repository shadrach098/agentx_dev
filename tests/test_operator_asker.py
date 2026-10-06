"""The built-in asker (ask_user=True): notebook, terminal, controlling terminal, headless."""

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
