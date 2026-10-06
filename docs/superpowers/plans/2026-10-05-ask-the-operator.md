# Ask the operator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Supervisor (sync and async) can ask the human operator for missing facts: the planner up front, every agent mid-run, through a built-in notebook/terminal-aware asker (`ask_user=True`) or the developer's own callback.

**Architecture:** One `OperatorChannel` per run (new module `agentx_dev/Operator.py`) owns the asker, the question budget, de-duplication, serialization, timeouts, events and records. The planner gets an optional `{"ask": [...]}` reply; agents get an `ask_user` tool attached per run (mirrors `attach_delegation`; planner/`delegate` helpers get it in `SpawnPolicy._build`). Answers travel as an "OPERATOR ANSWERS" block appended to the working task and to every dispatched step. The persistent deadline pauses while a question is open (`RunBudget.paused()` over a `PausableClock`).

**Tech Stack:** Python 3.10+, pydantic, pytest (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-10-05-ask-the-operator-design.md`

## Global Constraints

- Python 3.10, 3.11, 3.12 must work (CI matrix). No new dependencies.
- With `ask_user` unset (`None`/`False`), behavior is identical to today: no new prompt text, no new tools, no new events, `SupervisorResult.asked == []`. The existing suite (baseline: 767 passed, 4 skipped) must stay green.
- Sync and async parity: every behavior added to `Supervisor` is added to `AsyncSupervisor`.
- Never add a `Co-Authored-By` trailer or any attribution line to commit messages (the user is the sole contributor). Commit messages are plain conventional-commit text.
- Do not push, tag, publish, or touch `pypirc.ini`. Do not install packages. The 3.6.0 release is on hold.
- Do not change the version in `pyproject.toml` (stays 3.6.0; this ships inside 3.6.0).
- No emoji in anything the framework prints or sends to a model.
- Python code blocks in `docs/` and `README.md` must parse (`ast.parse`): the CI docs-syntax job checks them.
- Run the full suite from the worktree root with `python -m pytest -q`.
- Exact values from the spec: `max_questions` default `3`; `ask_timeout` default `None`; answer cut `2000` characters; controlling-terminal default timeout `300` seconds; tool name `ask_user`; reasons `no_channel`, `declined`, `timeout`, `limit`, `error`.

## Rulings (decided while writing this plan; they amend the spec and Task 7 edits the spec to match)

- **R1.** `ask_timeout` is not enforced on the notebook `input()` path: ipykernel's `input()` is not safe to call from a worker thread, and a person is looking at the input box (the Interrupt button works). It is enforced on the terminal and controlling-terminal paths and on custom callables. The controlling-terminal path defaults to 300 s when `ask_timeout` is `None`.
- **R2.** Registered specialists get the `ask_user` tool but not an addendum line (their prompts belong to the developer; the tool description carries the guidance). Planner/`delegate` helpers get the line "If a fact you need is missing and your tools cannot find it, call ask_user with one specific question. Do not guess." in their addendum, only when a channel exists.
- **R3.** A custom callable receives one string: the question, with `\n(context: <context>)` appended when the agent gave a context.
- **R4.** The question budget counts questions actually put to the operator, including ones that got no answer, so a failing callback is not hammered. De-duplicated repeats and `limit` replies do not count.
- **R5.** `OperatorChannel` serializes asks with one `threading.Lock` for both flows; the async path acquires it by polling (`acquire(blocking=False)` + `asyncio.sleep(0.02)`) so a cancelled task never leaks the lock and a sync specialist running in a thread (under `AsyncSupervisor`) is excluded too.

## File Structure

- Modify `agentx_dev/Runner/Persistence.py`: `PausableClock`, `RunBudget.paused()`.
- Create `agentx_dev/Operator.py`: built-in asker, `OperatorChannel`, planner helpers, `ask_user` tool, `attach_ask_user`, public `ask_human_tool`.
- Modify `agentx_dev/SubAgents.py`: `SpawnPolicy.operator`; helpers get the tool and the addendum line.
- Modify `agentx_dev/Supervisor.py`: constructor args, per-run channel, planner ask, answers block, events, `SupervisorResult.asked` (sync and async).
- Modify `agentx_dev/__init__.py`: export `ask_human_tool`.
- Tests: `tests/test_run_budget_pause.py`, `tests/test_operator_asker.py`, `tests/test_operator_channel.py`, `tests/test_operator_tool.py`, `tests/test_operator_supervisor.py`, `tests/test_operator_supervisor_async.py`.
- Docs/demo: `docs/guides/sub-agents.md`, `docs/cookbook/patterns.md` (pattern 32), `docs/cookbook/faq.md`, `docs/cookbook/troubleshooting.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`, `CHANGELOG.md`, `examples/subagents_demo.py`, `examples/mcp_github_triage_demo.py`, the spec, regenerated `host/data.js`.

---

### Task 1: A pausable run clock

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (imports near line 10-22; `RunBudget` at line 81)
- Test: `tests/test_run_budget_pause.py`

**Interfaces:**
- Consumes: existing `RunBudget(deadline, clock)`, `RunBudget.start(minutes, clock)`, `RunBudget.capped(minutes)`.
- Produces: `PausableClock(base=None)` (callable returning seconds; methods `pause()`, `resume()`); `RunBudget.paused()` context manager; `RunBudget.start()` with no clock now builds a `PausableClock`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_budget_pause.py`:

```python
"""RunBudget.paused(): time spent waiting on the operator does not count against the deadline."""

import pytest

from agentx_dev.Runner.Persistence import PausableClock, RunBudget


class Base:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_a_pausable_clock_stands_still_while_paused():
    base = Base()
    clock = PausableClock(base)
    base.t = 10.0
    assert clock() == 10.0
    clock.pause()
    base.t = 50.0
    assert clock() == 10.0
    clock.resume()
    assert clock() == 10.0
    base.t = 55.0
    assert clock() == 15.0


def test_pause_and_resume_are_idempotent():
    base = Base()
    clock = PausableClock(base)
    clock.resume()                       # not paused: nothing happens
    clock.pause()
    base.t = 5.0
    clock.pause()                        # a second pause must not restart the window
    base.t = 9.0
    clock.resume()
    clock.resume()
    assert clock() == 0.0


def test_paused_extends_the_run_budget_and_its_capped_children():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))       # 60 s
    child = budget.capped(0.5)                                # 30 s
    base.t = 20.0
    with budget.paused():
        base.t = 520.0
    assert budget.remaining() == pytest.approx(40.0)
    assert child.remaining() == pytest.approx(10.0)
    assert not budget.expired()


def test_time_outside_a_pause_still_counts():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))
    with budget.paused():
        base.t = 100.0
    base.t = 161.0
    assert budget.expired()


def test_paused_resumes_even_if_the_block_raises():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))
    with pytest.raises(RuntimeError):
        with budget.paused():
            base.t = 30.0
            raise RuntimeError("boom")
    base.t = 40.0
    assert budget.remaining() == pytest.approx(50.0)


def test_paused_is_a_no_op_on_a_plain_clock():
    budget = RunBudget(deadline=100.0, clock=lambda: 1.0)
    with budget.paused():
        pass
    assert budget.remaining() == 99.0


def test_a_default_budget_gets_a_pausable_clock():
    assert isinstance(RunBudget.start(1.0)._clock, PausableClock)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_run_budget_pause.py -q`
Expected: FAIL with `ImportError: cannot import name 'PausableClock'`.

- [ ] **Step 3: Implement**

In `agentx_dev/Runner/Persistence.py`, add `import threading` to the stdlib imports (after `import re`). Replace the `RunBudget` class (currently lines 81-105) with:

```python
class PausableClock:
    """``time.monotonic`` that stands still while paused. It is callable, so it drops in
    wherever a clock is expected, and every ``RunBudget`` made with it (including the
    children from ``capped``) shares the pause."""

    def __init__(self, base: Optional[Callable[[], float]] = None):
        self._base = base or time.monotonic
        self._lost = 0.0                       # total time spent paused so far
        self._paused_at: Optional[float] = None
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            now = self._base() if self._paused_at is None else self._paused_at
            return now - self._lost

    def pause(self) -> None:
        with self._lock:
            if self._paused_at is None:
                self._paused_at = self._base()

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                self._lost += self._base() - self._paused_at
                self._paused_at = None


class RunBudget:
    """A deadline on a monotonic clock, shared by a run and everything under it."""

    def __init__(self, deadline: float, clock: Optional[Callable[[], float]] = None):
        self.deadline = deadline
        self._clock = clock or time.monotonic

    @classmethod
    def start(cls, minutes: float, clock: Optional[Callable[[], float]] = None) -> "RunBudget":
        clock = clock or PausableClock()
        return cls(clock() + minutes * 60.0, clock)

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise BudgetExpired("time limit reached")

    def capped(self, minutes: float) -> "RunBudget":
        """A budget that ends at the earlier of this deadline and ``minutes`` from now."""
        return RunBudget(min(self.deadline, self._clock() + minutes * 60.0), self._clock)

    @contextmanager
    def paused(self):
        """Stop this run's clock for the length of the block (the operator is thinking).
        Does nothing when the clock cannot pause (a plain callable, as in some tests)."""
        pause = getattr(self._clock, "pause", None)
        resume = getattr(self._clock, "resume", None)
        if pause is None or resume is None:
            yield
            return
        pause()
        try:
            yield
        finally:
            resume()
```

- [ ] **Step 4: Run to verify it passes, then the persistence tests**

Run: `python -m pytest tests/test_run_budget_pause.py tests/test_persistence_core.py tests/test_persistence_run.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Runner/Persistence.py tests/test_run_budget_pause.py
git commit -m "feat(persistence): a pausable run clock, RunBudget.paused() for waiting on the operator"
```

---

### Task 2: The built-in asker

**Files:**
- Create: `agentx_dev/Operator.py`
- Test: `tests/test_operator_asker.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (module `agentx_dev.Operator`): constants `ASK_TOOL_NAME`, `MAX_ANSWER_CHARS`, `CONTROLLING_TTY_TIMEOUT`, `REASON_*`, `NO_ANSWER_TEXT`; exceptions `NoChannel`, `AskTimeout`; `builtin_asker(question, *, prefix="[agent]", timeout=None) -> Optional[str]` (raises `NoChannel` / `AskTimeout`, lets `KeyboardInterrupt` through, returns `None` on EOF); helpers `_in_notebook()`, `_notebook_can_prompt()`, `_stdin_is_tty()`, `_open_controlling_tty()`, `_call_with_timeout(fn, timeout)`, `_read_input(prompt)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_asker.py`:

```python
"""The built-in asker (ask_user=True): notebook, terminal, controlling terminal, headless."""

import builtins
import io
import sys
import threading
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
        out = io.StringIO()
        monkeypatch.setattr(op, "_open_controlling_tty", lambda: (io.StringIO("Coda\n"), out))
        assert op.builtin_asker("Which?") == "Coda"
        assert "Which?" in out.getvalue()

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_asker.py -q`
Expected: FAIL (`ImportError: cannot import name 'Operator'`).

- [ ] **Step 3: Implement**

Create `agentx_dev/Operator.py`:

```python
"""
Ask the operator: let a Supervisor, and the agents it runs, put a question to the person
who set the task, and carry the answer through the run.

``ask_user=True`` on a Supervisor uses the built-in asker below, which picks the channel by
where the code runs (notebook, terminal, IDE console) and never blocks in a headless
process. ``ask_user=<function>`` routes questions through the developer's own channel (a
chat UI, a websocket, a queue).
"""

from __future__ import annotations

import contextvars
import sys
import threading
from typing import Any, Callable, Optional

ASK_TOOL_NAME = "ask_user"
MAX_ANSWER_CHARS = 2000
CONTROLLING_TTY_TIMEOUT = 300.0            # seconds; the prompt can land in a window nobody sees

REASON_NO_CHANNEL = "no_channel"
REASON_DECLINED = "declined"
REASON_TIMEOUT = "timeout"
REASON_LIMIT = "limit"
REASON_ERROR = "error"

NO_ANSWER_TEXT = (
    "[no operator answer] Proceed on a stated assumption and say plainly what you assumed "
    "in your answer."
)


class NoChannel(Exception):
    """There is no way to reach an operator here (headless process, a kernel without stdin)."""


class AskTimeout(Exception):
    """The operator did not answer in time."""


# ----------------------------------------------------------------------------
# Where are we running?
# ----------------------------------------------------------------------------

def _ipython_shell() -> Any:
    getter = getattr(sys.modules.get("IPython"), "get_ipython", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _in_notebook() -> bool:
    """A Jupyter-style kernel (Jupyter, VS Code, Colab). Colab's shell subclasses
    ``ZMQInteractiveShell`` under another name, so walk the MRO."""
    shell = _ipython_shell()
    return shell is not None and any(c.__name__ == "ZMQInteractiveShell" for c in type(shell).__mro__)


def _notebook_can_prompt() -> bool:
    """False under papermill / nbconvert, where the kernel has stdin switched off."""
    kernel = getattr(_ipython_shell(), "kernel", None)
    return bool(getattr(kernel, "_allow_stdin", False))


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except Exception:
        return False


def _open_controlling_tty():
    """The process's controlling terminal: ``CONIN$``/``CONOUT$`` on Windows, ``/dev/tty``
    elsewhere. ``(None, None)`` when there is none (cron, Docker without ``-it``, a server)."""
    names = ("CONIN$", "CONOUT$") if sys.platform == "win32" else ("/dev/tty", "/dev/tty")
    tty_in = tty_out = None
    try:
        tty_in = open(names[0], "r")
        tty_out = open(names[1], "w")
        return tty_in, tty_out
    except OSError:
        for f in (tty_in, tty_out):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
        return None, None


# ----------------------------------------------------------------------------
# Reading an answer
# ----------------------------------------------------------------------------

def _call_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn()`` in a daemon thread (with the caller's contextvars) and wait at most
    ``timeout`` seconds. A thread stuck in a blocking read is abandoned: it ends with the
    process. Whatever ``fn`` raised is re-raised here."""
    box: dict = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as e:                         # re-raised in the caller
            box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AskTimeout(f"no answer within {timeout:g} seconds")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _read_input(prompt: str) -> Optional[str]:
    try:
        return input(prompt)
    except EOFError:
        return None
    except Exception as e:                                  # StdinNotImplementedError is a RuntimeError
        if type(e).__name__ == "StdinNotImplementedError":
            raise NoChannel("this kernel cannot take input") from e
        raise


def _ask_controlling_tty(question: str, prefix: str, timeout: Optional[float]) -> Optional[str]:
    tty_in, tty_out = _open_controlling_tty()
    if tty_in is None:
        raise NoChannel("no terminal to ask on")

    def read() -> Optional[str]:
        try:
            tty_out.write(f"\n{prefix} needs your input\n  question: {question}\n  > ")
            tty_out.flush()
            line = tty_in.readline()
        except EOFError:
            return None
        finally:
            for f in (tty_in, tty_out):
                try:
                    f.close()
                except Exception:
                    pass
        return line.strip() if line else None

    return _call_with_timeout(read, CONTROLLING_TTY_TIMEOUT if timeout is None else timeout)


def builtin_asker(question: str, *, prefix: str = "[agent]", timeout: Optional[float] = None) -> Optional[str]:
    """Ask the operator ``question`` on the channel this process has, in order:

    1. a notebook kernel: ``input()`` (the input box under the cell; ``timeout`` is not
       enforced here, a person is looking at the box and Interrupt works);
    2. a terminal: ``input()``;
    3. a controlling terminal when stdin is not one (IDE run panel, wrapper subprocess),
       with a 300 s default timeout because the prompt can land in a window nobody sees;
    4. nothing (headless): :class:`NoChannel`.

    Returns the typed answer, or ``None`` on EOF. ``KeyboardInterrupt`` is not swallowed:
    pressing Interrupt means stop the run. Raises :class:`NoChannel` / :class:`AskTimeout`.
    """
    if _in_notebook():
        if not _notebook_can_prompt():
            raise NoChannel("this notebook kernel cannot take input")
        return _read_input(f"{question}\n> ")
    if _stdin_is_tty():
        prompt = f"\n{prefix} needs your input\n  question: {question}\n> "
        if timeout is None:
            return _read_input(prompt)
        return _call_with_timeout(lambda: _read_input(prompt), timeout)
    return _ask_controlling_tty(question, prefix, timeout)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_operator_asker.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Operator.py tests/test_operator_asker.py
git commit -m "feat(operator): a built-in asker that picks notebook, terminal or controlling terminal"
```

---

### Task 3: `OperatorChannel` and the planner helpers

**Files:**
- Modify: `agentx_dev/Operator.py` (append)
- Test: `tests/test_operator_channel.py`

**Interfaces:**
- Consumes (Task 1): `RunBudget.paused()`. (Task 2): `builtin_asker`, `NoChannel`, `AskTimeout`, `_call_with_timeout`, `REASON_*`, `MAX_ANSWER_CHARS`.
- Produces: `Reply(text, reason, deduped)` with property `answered`; `validate_ask_user(ask_user, is_async)`; `OperatorChannel.create(ask_user, *, max_questions=3, timeout=None, is_async=False, verbose=False) -> Optional[OperatorChannel]`; channel methods `ask(question, context="", source="agent") -> Reply`, `async aask(...)`, `remaining() -> int`, `asked` (property, int), `has_answers() -> bool`, `answers_block() -> str`, `with_answers(text) -> str`, `emit(event)`, `drain() -> list`, attributes `records` (list of `{"source","question","answered","reason","deduped"}`), `events`, `budget`; `ask_instruction(limit) -> str`; `parse_ask_request(raw, limit) -> list[{"question","why"}]`; `NO_ANSWER_PLAN_NOTE`; `reply_text(reply) -> str`; `_shown(question, context)`; `_clean_answer(raw) -> Reply`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_channel.py`:

```python
"""OperatorChannel: the question budget, de-duplication, serialization, timeouts, events, pause."""

import asyncio
import threading
import time

import pytest

from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel
from agentx_dev.Runner.Persistence import PausableClock, RunBudget


def make(fn, **kw):
    return OperatorChannel.create(fn, **kw)


class TestCreate:
    def test_off_values_make_no_channel(self):
        assert OperatorChannel.create(None) is None
        assert OperatorChannel.create(False) is None

    def test_true_makes_the_builtin_channel(self):
        assert OperatorChannel.create(True) is not None

    def test_anything_else_that_is_not_callable_is_a_type_error(self):
        with pytest.raises(TypeError):
            OperatorChannel.create("yes")
        with pytest.raises(TypeError):
            op.validate_ask_user(3, is_async=False)

    def test_an_async_function_needs_the_async_supervisor(self):
        async def ask(q):
            return "x"
        with pytest.raises(TypeError, match="async"):
            OperatorChannel.create(ask, is_async=False)
        assert OperatorChannel.create(ask, is_async=True) is not None


class TestAsk:
    def test_an_answer_is_returned_recorded_and_carried_in_the_block(self):
        ch = make(lambda q: "Notion, Obsidian, Coda")
        reply = ch.ask("Which three competitors?", source="planner")
        assert reply.answered and reply.text == "Notion, Obsidian, Coda" and reply.reason is None
        assert ch.records == [{"source": "planner", "question": "Which three competitors?",
                               "answered": True, "reason": None, "deduped": False}]
        assert ch.has_answers()
        assert ch.answers_block() == (
            "OPERATOR ANSWERS (from the person who set this task; treat as facts):\n"
            "Q: Which three competitors?\nA: Notion, Obsidian, Coda")
        assert ch.with_answers("task") == "task\n\n" + ch.answers_block()

    def test_with_no_answers_the_block_is_empty_and_the_task_unchanged(self):
        ch = make(lambda q: None)
        assert ch.answers_block() == "" and ch.with_answers("task") == "task" and not ch.has_answers()

    def test_the_callable_gets_the_question_with_the_context_appended(self):
        seen = []
        ch = make(lambda q: seen.append(q) or "a")
        ch.ask("Which file?", context="step 2 of the audit")
        assert seen == ["Which file?\n(context: step 2 of the audit)"]

    def test_none_empty_and_blank_answers_are_declined(self):
        for raw in (None, "", "   "):
            ch = make(lambda q, raw=raw: raw)
            reply = ch.ask("q")
            assert not reply.answered and reply.reason == "declined"
            assert not ch.has_answers()

    def test_an_answer_is_cut_at_2000_characters(self):
        ch = make(lambda q: "x" * 5000)
        assert len(ch.ask("q").text) == 2000

    def test_an_exception_is_an_error_not_a_crash(self):
        def boom(q):
            raise RuntimeError("chat server down")
        ch = make(boom)
        reply = ch.ask("q")
        assert not reply.answered and reply.reason == "error"

    def test_the_builtin_channel_with_nowhere_to_ask_is_no_channel(self, monkeypatch):
        def nowhere(question, *, prefix="", timeout=None):
            raise op.NoChannel("headless")
        monkeypatch.setattr(op, "builtin_asker", nowhere)
        reply = make(True).ask("q")
        assert not reply.answered and reply.reason == "no_channel"

    def test_a_builtin_timeout_is_a_timeout(self, monkeypatch):
        def slow(question, *, prefix="", timeout=None):
            raise op.AskTimeout("slow")
        monkeypatch.setattr(op, "builtin_asker", slow)
        assert make(True).ask("q").reason == "timeout"

    def test_keyboard_interrupt_propagates_and_releases_the_lock(self):
        calls = []

        def interrupt(q):
            calls.append(q)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return "fine"
        ch = make(interrupt)
        with pytest.raises(KeyboardInterrupt):
            ch.ask("first")
        assert ch.ask("second").text == "fine"             # the lock was released

    def test_an_empty_question_is_declined_without_asking(self):
        ch = make(lambda q: pytest.fail("must not ask"))
        assert ch.ask("   ").reason == "declined" and ch.records == []

    def test_a_sync_timeout_abandons_a_stuck_callback(self):
        gate = threading.Event()
        ch = make(lambda q: gate.wait(5) and "late", timeout=0.05)
        try:
            reply = ch.ask("q")
        finally:
            gate.set()
        assert not reply.answered and reply.reason == "timeout"


class TestBudgetAndDedupe:
    def test_the_question_budget_is_shared_and_a_spent_budget_gives_limit(self):
        calls = []
        ch = make(lambda q: calls.append(q) or "a", max_questions=2)
        assert ch.ask("one").answered and ch.ask("two").answered
        third = ch.ask("three")
        assert not third.answered and third.reason == "limit"
        assert calls == ["one", "two"] and ch.remaining() == 0 and ch.asked == 2

    def test_zero_questions_means_every_ask_is_limit(self):
        ch = make(lambda q: pytest.fail("must not ask"), max_questions=0)
        assert ch.ask("q").reason == "limit"

    def test_a_repeat_gets_the_first_answer_without_using_a_slot(self):
        calls = []
        ch = make(lambda q: calls.append(q) or "Notion", max_questions=1)
        first = ch.ask("Which  competitors?")
        again = ch.ask("  which competitors?  ", source="helper")
        assert calls == ["Which competitors?"]
        assert again.text == "Notion" and again.deduped and not first.deduped
        assert ch.records[1]["deduped"] is True and ch.records[1]["source"] == "helper"
        assert ch.remaining() == 0
        assert ch.answers_block().count("Q:") == 1          # carried once

    def test_a_failed_ask_still_uses_a_slot_and_is_not_asked_again(self):
        calls = []

        def fail(q):
            calls.append(q)
            raise RuntimeError("down")
        ch = make(fail, max_questions=3)
        ch.ask("q")
        again = ch.ask("q")
        assert calls == ["q"] and again.reason == "error" and again.deduped and ch.asked == 1


class TestEvents:
    def test_question_and_answer_events_carry_no_answer_text(self):
        ch = make(lambda q: "secret value")
        ch.ask("Which?", context="ctx", source="planner")
        events = ch.drain()
        assert [e["type"] for e in events] == ["question", "answer"]
        assert events[0] == {"type": "question", "source": "planner", "question": "Which?", "context": "ctx"}
        assert events[1] == {"type": "answer", "source": "planner", "answered": True, "reason": None}
        assert "secret value" not in str(events)
        assert ch.drain() == []

    def test_verbose_prints_ask_lines(self, capsys):
        ch = make(lambda q: "a", verbose=True)
        ch.ask("Which?", source="planner")
        out = capsys.readouterr().out
        assert "[ask] planner asks: Which?" in out and "answered" in out


class TestPause:
    def test_waiting_on_the_operator_does_not_spend_the_deadline(self):
        class Base:
            t = 0.0

            def __call__(self):
                return self.t
        base = Base()
        budget = RunBudget.start(1.0, PausableClock(base))
        child = budget.capped(0.5)

        def slow_human(q):
            base.t += 500.0
            return "finally"
        ch = make(slow_human)
        ch.budget = budget
        base.t = 10.0
        assert ch.ask("q").answered
        assert budget.remaining() == pytest.approx(50.0)
        assert child.remaining() == pytest.approx(20.0)


class TestAsync:
    def test_an_async_function_is_awaited(self):
        async def ask(q):
            await asyncio.sleep(0)
            return "async answer"
        ch = make(ask, is_async=True)
        reply = asyncio.run(ch.aask("q", source="agent"))
        assert reply.text == "async answer"

    def test_a_sync_function_runs_off_the_event_loop(self):
        seen = {}

        def ask(q):
            seen["thread"] = threading.get_ident()
            return "sync answer"
        ch = make(ask, is_async=True)
        reply = asyncio.run(ch.aask("q"))
        assert reply.text == "sync answer" and seen["thread"] != threading.get_ident()

    def test_an_async_timeout_is_a_timeout(self):
        async def never(q):
            await asyncio.sleep(5)
        ch = make(never, is_async=True, timeout=0.05)
        assert asyncio.run(ch.aask("q")).reason == "timeout"

    def test_an_async_exception_is_an_error(self):
        async def boom(q):
            raise RuntimeError("down")
        ch = make(boom, is_async=True)
        assert asyncio.run(ch.aask("q")).reason == "error"

    def test_two_different_questions_are_asked_one_at_a_time(self):
        active = peak = 0
        lock = threading.Lock()

        def ask(q):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "ok"
        ch = make(ask, is_async=True)

        async def go():
            return await asyncio.gather(ch.aask("question one"), ch.aask("question two"))
        replies = asyncio.run(go())
        assert [r.answered for r in replies] == [True, True] and peak == 1

    def test_the_same_question_from_two_helpers_is_asked_once(self):
        calls = []

        def ask(q):
            calls.append(q)
            time.sleep(0.05)
            return "Notion"
        ch = make(ask, is_async=True)

        async def go():
            return await asyncio.gather(ch.aask("Which competitors?", source="a"),
                                        ch.aask("which competitors?", source="b"))
        first, second = asyncio.run(go())
        assert len(calls) == 1 and first.text == second.text == "Notion"
        assert sorted(r["deduped"] for r in ch.records) == [False, True]

    def test_a_sync_thread_and_the_event_loop_exclude_each_other(self):
        active = peak = 0
        lock = threading.Lock()

        def ask(q):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "ok"
        ch = make(ask, is_async=True)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("sync", ch.ask("from a thread")))
        t.start()

        async def go():
            return await ch.aask("from the loop")
        asyncio.run(go())
        t.join()
        assert out["sync"].answered and peak == 1


class TestPlannerHelpers:
    def test_ask_instruction_states_the_limit(self):
        text = op.ask_instruction(2)
        assert '"ask"' in text and "at most 2 questions" in text

    def test_parse_ask_request_takes_questions_up_to_the_limit(self):
        raw = [{"question": "Which competitors?", "why": "none named"}, "Which file?",
               {"question": "  "}, {"nope": 1}, 7, {"question": "Third?"}]
        assert op.parse_ask_request(raw, 2) == [
            {"question": "Which competitors?", "why": "none named"},
            {"question": "Which file?", "why": ""}]
        assert op.parse_ask_request(raw, 10)[-1] == {"question": "Third?", "why": ""}

    def test_parse_ask_request_rejects_anything_that_is_not_a_list(self):
        assert op.parse_ask_request("what?", 3) == [] and op.parse_ask_request(None, 3) == []
        assert op.parse_ask_request([{"question": "q"}], 0) == []

    def test_reply_text(self):
        assert op.reply_text(op.Reply(text="Notion")) == "[operator] Notion"
        assert op.reply_text(op.Reply(None, "declined")) == op.NO_ANSWER_TEXT
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_channel.py -q`
Expected: FAIL (`AttributeError`/`ImportError` for `OperatorChannel`).

- [ ] **Step 3: Implement**

In `agentx_dev/Operator.py`, replace the import block with the one below, then append the new code after `builtin_asker`.

Imports (replace the existing `from typing ...` line and add the others):

```python
import asyncio
import contextvars
import dataclasses
import inspect
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentx_dev.Tools import logger
```

Append:

```python
# ----------------------------------------------------------------------------
# Replies
# ----------------------------------------------------------------------------

@dataclass
class Reply:
    """What the operator said, or why nothing came back."""

    text: Optional[str] = None
    reason: Optional[str] = None          # None when answered; else one of the REASON_* words
    deduped: bool = False

    @property
    def answered(self) -> bool:
        return self.text is not None


def _clean_answer(raw: Any) -> Reply:
    if raw is None:
        return Reply(None, REASON_DECLINED)
    text = str(raw).strip()
    if not text:
        return Reply(None, REASON_DECLINED)
    return Reply(text[:MAX_ANSWER_CHARS])


def _normalize(question: str) -> str:
    return " ".join(str(question).split()).casefold()


def _shown(question: str, context: str = "") -> str:
    """What the asker receives: the question, with the agent's one-line context appended."""
    return f"{question}\n(context: {context})" if context else question


def reply_text(reply: Reply) -> str:
    """The tool result an agent sees. Never an error, so its stuck logic is not triggered."""
    return f"[operator] {reply.text}" if reply.answered else NO_ANSWER_TEXT


def validate_ask_user(ask_user: Any, is_async: bool) -> Any:
    """Check an ``ask_user=`` value at construction time and return it unchanged."""
    if ask_user is None or ask_user is False or ask_user is True:
        return ask_user
    if not callable(ask_user):
        raise TypeError("ask_user must be True, False/None, or a function (question) -> answer")
    if not is_async and asyncio.iscoroutinefunction(ask_user):
        raise TypeError("ask_user is an async function: use AsyncSupervisor, or pass a plain function")
    return ask_user


# ----------------------------------------------------------------------------
# The channel: the one place that knows about the operator
# ----------------------------------------------------------------------------

class OperatorChannel:
    """One per Supervisor run. Owns the asker, the question budget, de-duplication,
    one-at-a-time asking, timeouts, the event buffer and the records.

    ``ask`` / ``aask`` never raise because of the operator channel (a callback that raises or
    times out is a no-answer); only ``KeyboardInterrupt`` and task cancellation propagate.
    """

    def __init__(self, asker: Optional[Callable[[str], Any]], *, builtin: bool = False,
                 max_questions: int = 3, timeout: Optional[float] = None,
                 is_async: bool = False, verbose: bool = False, prefix: str = "[agent]"):
        self._asker = asker
        self._builtin = builtin
        self.max_questions = max(0, int(max_questions))
        self.timeout = timeout
        self.is_async = is_async
        self.verbose = verbose
        self.prefix = prefix
        self.budget: Any = None                       # the run's RunBudget; paused while a question is open
        self.records: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._answers: List[Tuple[str, str]] = []
        self._seen: Dict[str, Reply] = {}
        self._asked = 0
        self._lock = threading.Lock()                 # asks are one at a time, sync and async alike

    @classmethod
    def create(cls, ask_user: Any, *, max_questions: int = 3, timeout: Optional[float] = None,
               is_async: bool = False, verbose: bool = False) -> Optional["OperatorChannel"]:
        """The channel for an ``ask_user=`` value, or ``None`` when asking is off."""
        validate_ask_user(ask_user, is_async)
        if ask_user is None or ask_user is False:
            return None
        if ask_user is True:
            return cls(None, builtin=True, max_questions=max_questions, timeout=timeout,
                       is_async=is_async, verbose=verbose)
        return cls(ask_user, max_questions=max_questions, timeout=timeout,
                   is_async=is_async, verbose=verbose)

    # -- state ----------------------------------------------------------------

    @property
    def asked(self) -> int:
        return self._asked

    def remaining(self) -> int:
        return max(0, self.max_questions - self._asked)

    def has_answers(self) -> bool:
        return bool(self._answers)

    def answers_block(self) -> str:
        if not self._answers:
            return ""
        lines = ["OPERATOR ANSWERS (from the person who set this task; treat as facts):"]
        for question, answer in self._answers:
            lines += [f"Q: {question}", f"A: {answer}"]
        return "\n".join(lines)

    def with_answers(self, text: str) -> str:
        block = self.answers_block()
        return f"{text}\n\n{block}" if block else text

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def drain(self) -> List[Dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"[ask] {text}")

    # -- asking ---------------------------------------------------------------

    def _begin(self, question: str, key: str, context: str, source: str) -> Optional[Reply]:
        """Dedupe and budget checks, under the lock. A final reply, or ``None`` when the
        operator must be asked (the slot is taken and the question event is out)."""
        hit = self._seen.get(key)
        if hit is not None:
            return self._finish(source, question, hit, deduped=True)
        if self._asked >= self.max_questions:
            return self._finish(source, question, Reply(None, REASON_LIMIT))
        self._asked += 1
        self.emit({"type": "question", "source": source, "question": question, "context": context})
        self._say(f"{source} asks: {question}")
        return None

    def _end(self, question: str, key: str, source: str, reply: Reply) -> Reply:
        self._seen[key] = reply
        return self._finish(source, question, reply)

    def _finish(self, source: str, question: str, reply: Reply, *, deduped: bool = False) -> Reply:
        out = dataclasses.replace(reply, deduped=deduped)
        self.records.append({"source": source, "question": question, "answered": out.answered,
                             "reason": out.reason, "deduped": deduped})
        self.emit({"type": "answer", "source": source, "answered": out.answered, "reason": out.reason})
        if out.answered and not deduped:
            self._answers.append((question, out.text))
        self._say("answered" if out.answered else f"no answer ({out.reason})")
        return out

    def _paused(self):
        return self.budget.paused() if self.budget is not None else nullcontext()

    def ask(self, question: str, context: str = "", source: str = "agent") -> Reply:
        q = " ".join(str(question or "").split())
        if not q:
            return Reply(None, REASON_DECLINED)
        key = _normalize(q)
        with self._lock:
            done = self._begin(q, key, context, source)
            if done is not None:
                return done
            return self._end(q, key, source, self._ask_sync(_shown(q, context)))

    async def aask(self, question: str, context: str = "", source: str = "agent") -> Reply:
        q = " ".join(str(question or "").split())
        if not q:
            return Reply(None, REASON_DECLINED)
        key = _normalize(q)
        while not self._lock.acquire(blocking=False):      # never blocks the loop; a cancel cannot leak the lock
            await asyncio.sleep(0.02)
        try:
            done = self._begin(q, key, context, source)
            if done is not None:
                return done
            return self._end(q, key, source, await self._ask_async(_shown(q, context)))
        finally:
            self._lock.release()

    def _ask_sync(self, shown: str) -> Reply:
        try:
            with self._paused():
                if self._builtin:
                    raw = builtin_asker(shown, prefix=self.prefix, timeout=self.timeout)
                elif self.timeout is not None:
                    raw = _call_with_timeout(lambda: self._asker(shown), self.timeout)
                else:
                    raw = self._asker(shown)
        except NoChannel:
            return Reply(None, REASON_NO_CHANNEL)
        except AskTimeout:
            return Reply(None, REASON_TIMEOUT)
        except Exception as e:                             # not KeyboardInterrupt / CancelledError
            logger.warning(f"ask_user raised; treating as no answer: {e}")
            return Reply(None, REASON_ERROR)
        if inspect.isawaitable(raw):
            getattr(raw, "close", lambda: None)()
            logger.warning("ask_user returned an awaitable on a sync Supervisor; treating as no answer")
            return Reply(None, REASON_ERROR)
        return _clean_answer(raw)

    async def _ask_async(self, shown: str) -> Reply:
        async def go() -> Any:
            if asyncio.iscoroutinefunction(self._asker):
                raw = await self._asker(shown)
            else:
                raw = await asyncio.to_thread(self._asker, shown)
            return await raw if inspect.isawaitable(raw) else raw

        try:
            with self._paused():
                if self._builtin:
                    # input() on the loop thread: other tasks wait while the person types.
                    raw = builtin_asker(shown, prefix=self.prefix, timeout=self.timeout)
                else:
                    raw = await asyncio.wait_for(go(), self.timeout)
        except NoChannel:
            return Reply(None, REASON_NO_CHANNEL)
        except (AskTimeout, asyncio.TimeoutError):
            return Reply(None, REASON_TIMEOUT)
        except Exception as e:
            logger.warning(f"ask_user raised; treating as no answer: {e}")
            return Reply(None, REASON_ERROR)
        return _clean_answer(raw)


# ----------------------------------------------------------------------------
# The planner may ask before it plans
# ----------------------------------------------------------------------------

_ASK_INSTRUCTION = """

── ASKING THE OPERATOR ──────────────────────────────────────────────
You may ask the operator instead of planning, but ONLY when the task leaves out a fact you cannot reasonably assume and a wrong guess would waste the whole run (for example: which three competitors, which file, which account). Then reply with {"ask": [{"question": "...", "why": "..."}]} and no plan: at most <<N>> questions, each specific and self-contained. If a sensible assumption lets you proceed, write the plan instead.
────────────────────────────────────────────────────────────────────
"""

NO_ANSWER_PLAN_NOTE = (
    "\n\nNo operator answered your questions. Plan on stated assumptions and make each "
    "assumption explicit in the step queries."
)


def ask_instruction(limit: int) -> str:
    """The planner-prompt block that offers the ``ask`` reply."""
    return _ASK_INSTRUCTION.replace("<<N>>", str(limit))


def parse_ask_request(raw: Any, limit: int) -> List[Dict[str, str]]:
    """The planner's ``ask`` value as ``[{"question", "why"}]``, at most ``limit`` long.
    Anything that is not a list of questions gives ``[]``."""
    if not isinstance(raw, list) or limit <= 0:
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            question, why = item, ""
        elif isinstance(item, dict) and isinstance(item.get("question"), str):
            question, why = item["question"], str(item.get("why") or "")
        else:
            continue
        question = " ".join(question.split())
        if question:
            out.append({"question": question, "why": " ".join(why.split())})
    return out[:limit]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_operator_channel.py tests/test_operator_asker.py -q`
Expected: all pass. (The two timeout tests wait about 50 ms each; the stuck thread is released by the test's `gate.set()`.)

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Operator.py tests/test_operator_channel.py
git commit -m "feat(operator): OperatorChannel with budget, dedupe, timeout, events and a pausable clock hook"
```

---

### Task 4: The `ask_user` tool, helper wiring and the public `ask_human_tool`

**Files:**
- Modify: `agentx_dev/Operator.py` (append; add imports)
- Modify: `agentx_dev/SubAgents.py` (`SpawnPolicy.__init__` ~line 361; `_make_runner` ~line 479; `_build` ~line 552; imports ~line 30)
- Modify: `agentx_dev/__init__.py` (import near line 72; `__all__` near line 229)
- Test: `tests/test_operator_tool.py`

**Interfaces:**
- Consumes (Task 3): `OperatorChannel.ask/aask`, `reply_text`, `Reply`, `_clean_answer`, `_shown`. (Task 2): `builtin_asker`, `NoChannel`, `AskTimeout`, `NO_ANSWER_TEXT`, `ASK_TOOL_NAME`.
- Produces: `AskUserArgs`, `ASK_USER_DESCRIPTION`, `ASK_ADDENDUM_LINE`, `make_ask_tool(parent, channel, source) -> tool` (async parent gets an `AsyncStructuredTool`, `cacheable=False`), `attach_ask_user(agents: dict[str, runner], channel_or_None)` context manager, `ask_human_tool(*, prompt_prefix="[agent]", ask_timeout=None)` (exported from `agentx_dev`); `SpawnPolicy(..., operator=None)` attribute `operator`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_tool.py`:

```python
"""The ask_user tool, attach_ask_user, helper wiring, and the public ask_human_tool."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, StandardTool
from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel, attach_ask_user, make_ask_tool
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router


def runner(model=None):
    return AgentRunner(model=model or router(), agent=AgentType.ReAct, tools=[], verbose=False)


def arunner(model=None):
    return AsyncAgentRunner(model=model or router(), agent=AgentType.ReAct, tools=[], verbose=False)


class TestAttach:
    def test_the_tool_is_attached_for_the_block_and_removed_after(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "Notion")
        with attach_ask_user({"worker": r}, ch):
            assert r.registry.has("ask_user")
            assert r.registry.dispatch("ask_user", {"question": "Which?"}) == "[operator] Notion"
        assert not r.registry.has("ask_user")

    def test_the_tool_is_removed_even_if_the_block_raises(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "x")
        with pytest.raises(RuntimeError):
            with attach_ask_user({"worker": r}, ch):
                raise RuntimeError("boom")
        assert not r.registry.has("ask_user")

    def test_no_channel_attaches_nothing(self):
        r = runner()
        with attach_ask_user({"worker": r}, None):
            assert not r.registry.has("ask_user")

    def test_a_runner_with_its_own_ask_user_tool_is_left_alone(self):
        mine = StandardTool(func=lambda question: "mine", name="ask_user", description="my own")
        r = AgentRunner(model=router(), agent=AgentType.ReAct, tools=[mine], verbose=False)
        ch = OperatorChannel.create(lambda q: "framework")
        with attach_ask_user({"worker": r}, ch):
            assert r.registry.dispatch("ask_user", {"question": "q"}) == "mine"
        assert r.registry.has("ask_user")                 # still the developer's tool

    def test_objects_that_cannot_take_a_tool_are_skipped(self):
        class Plain:
            pass
        ch = OperatorChannel.create(lambda q: "x")
        with attach_ask_user({"plain": Plain()}, ch):
            pass

    def test_the_tool_is_never_cached(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "x")
        with attach_ask_user({"worker": r}, ch):
            tool = next(t for t in r.tools if t.name == "ask_user")
            assert tool.cacheable is False


class TestTheToolAnswers:
    def test_no_answer_is_a_plain_note_not_an_error(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: None)
        with attach_ask_user({"worker": r}, ch):
            out = r.registry.dispatch("ask_user", {"question": "q"})
        assert out == op.NO_ANSWER_TEXT

    def test_the_source_is_the_agent_name_and_the_context_is_passed(self):
        r = runner()
        seen = []
        ch = OperatorChannel.create(lambda q: seen.append(q) or "a")
        with attach_ask_user({"researcher": r}, ch):
            r.registry.dispatch("ask_user", {"question": "Which?", "context": "step 1"})
        assert seen == ["Which?\n(context: step 1)"]
        assert ch.records[0]["source"] == "researcher"

    def test_a_model_driven_agent_asks_and_uses_the_answer(self):
        turns = []

        def script(messages):
            turns.append(messages)
            if len(turns) == 1:
                return make_react_response("ask_user", {"question": "Which competitors?"})
            return make_final("compared " + str(messages))
        r = runner(MockModel(script=script))
        ch = OperatorChannel.create(lambda q: "Notion, Obsidian, Coda")
        with attach_ask_user({"worker": r}, ch):
            result = r.invoke("Compare our competitors")
        assert "Notion, Obsidian, Coda" in result.content
        assert "[operator] Notion, Obsidian, Coda" in str(turns[1])

    def test_an_async_runner_gets_an_async_tool(self):
        r = arunner()
        ch = OperatorChannel.create(lambda q: "Coda", is_async=True)
        with attach_ask_user({"worker": r}, ch):
            out = asyncio.run(r.registry.adispatch("ask_user", {"question": "Which?"}))
        assert out == "[operator] Coda" and not r.registry.has("ask_user")


class TestHelpers:
    def spec(self):
        return AgentSpec(name="helper", instructions="You help.", tools=("web",), origin="plan")

    def test_a_helper_built_with_a_channel_gets_the_tool_and_the_addendum_line(self):
        ch = OperatorChannel.create(lambda q: "x")
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(), operator=ch)
        built = policy.build(self.spec())
        assert built.runner.registry.has("ask_user")
        assert op.ASK_ADDENDUM_LINE.strip() in built.runner.system_addendum

    def test_a_helper_built_without_a_channel_does_not(self):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router())
        built = policy.build(self.spec())
        assert not built.runner.registry.has("ask_user")
        assert "ask_user" not in built.runner.system_addendum

    def test_an_async_helper_gets_an_async_tool(self):
        ch = OperatorChannel.create(lambda q: "Coda", is_async=True)
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(),
                             is_async=True, operator=ch)
        built = policy.build(self.spec())
        out = asyncio.run(built.runner.registry.adispatch("ask_user", {"question": "Which?"}))
        assert out == "[operator] Coda"

    def test_a_pool_tool_named_ask_user_refuses_the_helper_cleanly(self):
        from agentx_dev.SubAgents import SpawnRefused
        clash = StandardTool(func=lambda question: "pool", name="ask_user", description="pool tool")
        ch = OperatorChannel.create(lambda q: "x")
        cfg = SpawnConfig(enabled=True, tools=[clash], capabilities={"web"})
        policy = SpawnPolicy(cfg, router(), operator=ch)
        with pytest.raises(SpawnRefused):
            policy.build(AgentSpec(name="h", instructions="x", tools=("ask_user",), origin="plan"))


class TestAskHumanTool:
    def test_it_is_exported_and_asks_through_the_builtin_asker(self, monkeypatch):
        import agentx_dev
        assert agentx_dev.ask_human_tool is op.ask_human_tool
        seen = []
        monkeypatch.setattr(op, "builtin_asker",
                            lambda q, *, prefix="", timeout=None: seen.append((q, prefix, timeout)) or "Notion")
        tool = op.ask_human_tool(prompt_prefix="[triager]", ask_timeout=9.0)
        assert tool.name == "ask_human"
        assert tool.func(question="Which?", context="c") == "[operator] Notion"
        assert seen == [("Which?\n(context: c)", "[triager]", 9.0)]

    def test_no_channel_and_timeout_and_blank_give_the_no_answer_note(self, monkeypatch):
        tool = op.ask_human_tool()
        for outcome in (op.NoChannel("x"), op.AskTimeout("x")):
            def fail(q, *, prefix="", timeout=None, outcome=outcome):
                raise outcome
            monkeypatch.setattr(op, "builtin_asker", fail)
            assert tool.func(question="q") == op.NO_ANSWER_TEXT
        monkeypatch.setattr(op, "builtin_asker", lambda q, *, prefix="", timeout=None: "  ")
        assert tool.func(question="q") == op.NO_ANSWER_TEXT
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_tool.py -q`
Expected: FAIL (`ImportError: cannot import name 'attach_ask_user'`).

- [ ] **Step 3: Implement**

In `agentx_dev/Operator.py`, add to the imports: `from contextlib import contextmanager, nullcontext` (extend the existing contextlib line), `from pydantic import BaseModel, Field`, `from agentx_dev.AsyncTools import AsyncStructuredTool`, and change the tools import to `from agentx_dev.Tools import StructuredTool, logger`. Append:

```python
# ----------------------------------------------------------------------------
# The tool agents use to ask
# ----------------------------------------------------------------------------

class AskUserArgs(BaseModel):
    question: str = Field(
        ...,
        description=("The question to ask the operator. Be specific and self-contained: they "
                     "cannot see your conversation. Ask ONE thing at a time."),
    )
    context: str = Field(
        "",
        description=("Optional one line of context shown with the question so the operator "
                     "knows what stage of the task you are at."),
    )


ASK_USER_DESCRIPTION = (
    "Ask the operator ONE question when a fact you need is missing and your tools cannot find "
    "it. Good: which three competitors, which file, which account. Bad: asking permission for "
    "each step, tone or audience, or anything you can look up. Returns the operator's reply, or "
    "a note that nobody answered (then proceed on a stated assumption)."
)

ASK_ADDENDUM_LINE = (
    "\n\n- If a fact you need is missing and your tools cannot find it, call ask_user with one "
    "specific question. Do not guess."
)


def _is_async_runner(runner: Any) -> bool:
    return asyncio.iscoroutinefunction(getattr(runner, "Initialize", None))


def make_ask_tool(parent: Any, channel: OperatorChannel, source: str) -> Any:
    """The ``ask_user`` tool for ``parent``. An async parent gets an async tool that awaits the
    channel; a sync parent a sync tool. ``source`` (the agent's name) labels events and records."""
    if _is_async_runner(parent):
        async def ask_user(question: str, context: str = "") -> str:
            return reply_text(await channel.aask(question, context, source))

        tool = AsyncStructuredTool(func=ask_user, args_schema=AskUserArgs,
                                   name=ASK_TOOL_NAME, description=ASK_USER_DESCRIPTION)
    else:
        def ask_user(question: str, context: str = "") -> str:
            return reply_text(channel.ask(question, context, source))

        tool = StructuredTool(func=ask_user, args_schema=AskUserArgs,
                              name=ASK_TOOL_NAME, description=ASK_USER_DESCRIPTION)
    tool.cacheable = False            # asking has side effects: never answer it from the cache
    return tool


@contextmanager
def attach_ask_user(agents: Dict[str, Any], channel: Optional[OperatorChannel]):
    """Give every runner in ``agents`` (``name -> runner``) the ``ask_user`` tool for the block.

    Nothing is attached when ``channel`` is ``None``. Objects that cannot take a tool (no
    ``add_tool``), or that already have a tool named ``ask_user`` (the developer's wins), are
    left alone. The tool is removed on exit, even if the block raises, so the developer's
    runners are exactly as they were."""
    attached: List[Any] = []
    if channel is not None:
        for name, r in agents.items():
            if not callable(getattr(r, "add_tool", None)) or not hasattr(r, "registry"):
                continue
            if r.registry.has(ASK_TOOL_NAME):
                continue
            r.add_tool(make_ask_tool(r, channel, name))
            attached.append(r)
    try:
        yield
    finally:
        for r in attached:
            r.remove_tool(ASK_TOOL_NAME)


def ask_human_tool(*, prompt_prefix: str = "[agent]", ask_timeout: Optional[float] = None) -> Any:
    """A standalone ``ask_human`` tool for any ``AgentRunner``, built on the built-in asker:
    it prompts in the notebook's input box or the terminal, and never blocks in a headless
    process. Returns the operator's reply, or a note that nobody answered."""

    def _ask(question: str, context: str = "") -> str:
        try:
            raw = builtin_asker(_shown(question, context), prefix=prompt_prefix, timeout=ask_timeout)
        except (NoChannel, AskTimeout):
            return NO_ANSWER_TEXT
        return reply_text(_clean_answer(raw))

    return StructuredTool(
        func=_ask, args_schema=AskUserArgs, name="ask_human",
        description=(
            "Ask the operator ONE clarifying question when the task is genuinely ambiguous and a "
            "guess would waste a whole run. Good uses: acronym disambiguation, which file or "
            "account is meant. BAD uses: tone or audience, permission for every step, or asking "
            "the operator to do your research. Returns the operator's typed reply, or a note "
            "that nobody answered."
        ),
    )
```

In `agentx_dev/SubAgents.py`:

1. After the existing `from agentx_dev.Runner.Persistence import ...` line add:
   `from agentx_dev.Operator import ASK_ADDENDUM_LINE, make_ask_tool`
2. In `SpawnPolicy.__init__`, add a keyword-only parameter `operator: Any = None` (after `budget`) and, after `self.budget = budget`, add:
   ```python
           # The run's OperatorChannel (set by the Supervisor). When present, every helper this
           # policy builds gets the ask_user tool.
           self.operator = operator
   ```
3. In `_make_runner`, change the `system_addendum=` argument to:
   ```python
               system_addendum=(_SPAWNED_SPECIALIST_ADDENDUM
                                + (ASK_ADDENDUM_LINE if self.operator is not None else "")
                                + "\n\nYour role:\n" + spec.instructions),
   ```
4. In `_build`, immediately after the `if depth < cfg.max_depth:` block (before `granted = ...`), add:
   ```python
           if self.operator is not None:
               try:
                   runner.add_tool(make_ask_tool(runner, self.operator, spec.name))
               except Exception as e:                   # e.g. a pool tool already named "ask_user"
                   raise SpawnRefused(f"could not build the agent: {e}") from e
   ```

In `agentx_dev/__init__.py`: after `from .SubAgents import AgentSpec` add `from .Operator import ask_human_tool`; in `__all__` after `"SubtaskResult",` add `"ask_human_tool",`.

- [ ] **Step 4: Run to verify it passes, then the sub-agent suites**

Run: `python -m pytest tests/test_operator_tool.py tests/test_subagent_policy.py tests/test_subagent_delegate.py tests/test_subagent_delegate_async.py -q`
Expected: all pass. If an existing test constructs `SpawnPolicy` positionally past `budget`, it still works (`operator` is keyword-only).

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Operator.py agentx_dev/SubAgents.py agentx_dev/__init__.py tests/test_operator_tool.py
git commit -m "feat(operator): the ask_user tool, helpers get it, and a public notebook-aware ask_human_tool"
```

---

### Task 5: Wire the sync Supervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py` (imports ~line 36; `SupervisorResult` line 160; `_SpawnMixin` line 891; `Supervisor.__init__` line 970; `_plan_once` line 1097; `_plan` line 1130; `_synthesize` line 1265; `_run_plan` dispatched_query ~line 1447; `stream` line 1474)
- Test: `tests/test_operator_supervisor.py`

**Interfaces:**
- Consumes (Tasks 1-4): `OperatorChannel.create/ask/remaining/has_answers/with_answers/drain/budget/records`, `ask_instruction`, `parse_ask_request`, `NO_ANSWER_PLAN_NOTE`, `attach_ask_user`, `validate_ask_user`, `SpawnPolicy.operator`.
- Produces: `Supervisor(..., ask_user=None, max_questions=3, ask_timeout=None)`; `SupervisorResult.asked: List[Dict[str, Any]]`; mixin helpers `_new_operator_channel()`, `_task_for_model(user_task)`, `_with_answers(query)`, `_drain_operator_events()`, `_asked_records()`; instance attr `_operator`; `question`/`answer` stream events; `Supervisor._plan_once(user_task, repair_note="", ask_allowed=False)`. Task 6 mirrors all of this on `AsyncSupervisor`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_supervisor.py`:

```python
"""Supervisor + ask_user: the planner asks up front, agents ask mid-run, answers carry through."""

import json

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, Supervisor
from agentx_dev import Operator as op
from agentx_dev.SubAgents import SpawnConfig
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import ScriptedRunner, new_agent_step, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})
ANSWER = "Notion, Obsidian, Coda"
ASK_MARK = "ASKING THE OPERATOR"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return Supervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


class TestPlannerAsks:
    def test_the_planner_asks_then_replans_with_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "compare " + ANSWER))],
                       synth="Compared.")
        worker = ScriptedRunner()
        asked = []
        sup = supervisor(model, worker, ask_user=lambda q: asked.append(q) or ANSWER)
        result = sup.run("Compare the pricing pages of our three competitors")

        first, second = model.planner_prompts()
        assert ASK_MARK in first and "OPERATOR ANSWERS" not in first
        assert "OPERATOR ANSWERS" in second and ANSWER in second and ASK_MARK not in second
        assert asked == ["Which three competitors should I compare?"]
        assert result.query == "Compare the pricing pages of our three competitors"     # the original
        assert result.content == "Compared." and result.outcome == "done"
        assert result.asked == [{"source": "planner", "question": "Which three competitors should I compare?",
                                 "answered": True, "reason": None, "deduped": False}]

    def test_the_answers_reach_the_dispatched_step_and_synthesis(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "go"))])
        worker = ScriptedRunner()
        supervisor(model, worker, ask_user=lambda q: ANSWER).run("Compare our competitors")
        assert "OPERATOR ANSWERS" in worker.calls[0][0] and ANSWER in worker.calls[0][0]
        synth_prompt = next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))
        assert "OPERATOR ANSWERS" in synth_prompt and ANSWER in synth_prompt

    def test_no_answer_plans_on_assumptions(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = supervisor(model, ask_user=lambda q: None).run("Compare our competitors")
        second = model.planner_prompts()[1]
        assert "No operator answered" in second and "OPERATOR ANSWERS" not in second
        assert result.outcome == "done" and result.asked[0]["answered"] is False
        assert result.asked[0]["reason"] == "declined"

    def test_a_plan_wins_over_an_ask(self):
        both = json.dumps({"plan": [step("s1", "worker")], "ask": [{"question": "ignored?"}]})
        model = router(plans=[both])
        asked = []
        result = supervisor(model, ask_user=lambda q: asked.append(q) or "x").run("task")
        assert asked == [] and result.asked == [] and len(model.planner_prompts()) == 1

    def test_a_malformed_ask_is_the_existing_no_plan_failure(self):
        model = router(plans=[json.dumps({"ask": "what?"})])
        result = supervisor(model, ask_user=lambda q: "x").run("task")
        assert result.content == "Supervisor failed to produce a valid plan." and result.outcome == "stuck"

    def test_the_planner_cannot_ask_twice(self):
        model = router(plans=[ASK_PLAN, ASK_PLAN])
        asked = []
        result = supervisor(model, ask_user=lambda q: asked.append(q) or "x").run("task")
        assert len(asked) == 1 and result.outcome == "stuck"

    def test_the_question_budget_limits_what_the_planner_may_ask(self):
        two = json.dumps({"ask": [{"question": "one?"}, {"question": "two?"}]})
        model = router(plans=[two, plan_json(step("s1", "worker"))])
        asked = []
        supervisor(model, ask_user=lambda q: asked.append(q) or "x", max_questions=1).run("task")
        assert asked == ["one?"] and "at most 1 questions" in model.planner_prompts()[0]

    def test_zero_questions_never_offers_the_ask_option(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, ask_user=lambda q: "x", max_questions=0).run("task")
        assert ASK_MARK not in model.planner_prompts()[0]

    def test_events_show_the_question_and_whether_it_was_answered(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: ANSWER)
        events = list(sup.stream("task"))
        types = [e["type"] for e in events]
        assert types.index("question") < types.index("answer") < types.index("plan")
        question = next(e for e in events if e["type"] == "question")
        answer = next(e for e in events if e["type"] == "answer")
        assert question["source"] == "planner" and "Which three" in question["question"]
        assert answer["answered"] is True and ANSWER not in str(answer)


class TestOff:
    def test_unset_changes_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = supervisor(model, worker).run("task")
        assert ASK_MARK not in model.planner_prompts()[0]
        assert "OPERATOR" not in worker.calls[0][0] and result.asked == []
        assert not any(e["type"] in ("question", "answer") for e in supervisor(
            router(plans=[plan_json(step("s1", "worker"))]), None).stream("task"))

    def test_the_constructor_validates_ask_user(self):
        with pytest.raises(TypeError):
            supervisor(router(), ask_user="yes")

        async def ask(q):
            return "x"
        with pytest.raises(TypeError, match="async"):
            supervisor(router(), ask_user=ask)


def asking_worker(question="Which file?"):
    turns = []

    def script(messages):
        turns.append(1)
        if len(turns) == 1:
            return make_react_response("ask_user", {"question": question})
        return make_final("worker used: " + str(messages[-1]))
    return AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)


class TestAgentsAskMidRun:
    def test_a_registered_specialist_can_ask_and_is_restored_afterwards(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = asking_worker()
        asked = []
        result = supervisor(model, worker, ask_user=lambda q: asked.append(q) or "report.md").run("task")
        assert asked == ["Which file?"] and "report.md" in result.subtasks[0].content
        assert result.asked[0]["source"] == "worker" and result.asked[0]["answered"] is True
        assert not worker.registry.has("ask_user")

    def test_a_later_step_sees_the_answer_an_earlier_step_got(self):
        model = router(plans=[plan_json(step("s1", "worker"), step("s2", "scribe", deps=["s1"]))])
        scribe = ScriptedRunner()
        sup = Supervisor(model=model, verbose=False, ask_user=lambda q: "report.md",
                         agents={"worker": ("asks", asking_worker()), "scribe": ("writes", scribe)})
        sup.run("task")
        assert "OPERATOR ANSWERS" in scribe.calls[0][0] and "report.md" in scribe.calls[0][0]

    def test_a_planner_defined_helper_can_ask(self):
        turns = []

        def sub(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which three competitors?"})
                    if len(turns) == 1 else make_final("helper done"))
        model = router(plans=[plan_json(new_agent_step("s1", "analyst", "You analyse.", tools=["web"]))], sub=sub)
        asked = []
        sup = supervisor(model, spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                         ask_user=lambda q: asked.append(q) or ANSWER)
        result = sup.run("task")
        assert asked == ["Which three competitors?"]
        assert result.asked[0]["source"] == "analyst" and result.subtasks[0].content == "helper done"

    def test_the_same_question_from_two_steps_is_asked_once(self):
        model = router(plans=[plan_json(step("a", "w1"), step("b", "w2"))])
        asked = []
        sup = Supervisor(model=model, verbose=False, ask_user=lambda q: asked.append(q) or "report.md",
                         agents={"w1": ("one", asking_worker()), "w2": ("two", asking_worker("which file?"))})
        result = sup.run("task")
        assert len(asked) == 1
        assert sorted(r["deduped"] for r in result.asked) == [False, True]


class TestPersistent:
    def test_the_run_budget_is_wired_to_the_channel_so_waiting_is_free(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: "x", persistence=Persistence(max_minutes=5))
        list(sup.stream("task"))
        from agentx_dev.Runner.Persistence import PausableClock
        assert sup._operator.budget is not None and isinstance(sup._operator.budget._clock, PausableClock)

    def test_recovery_planning_and_helpers_get_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        sup = supervisor(model, worker, ask_user=lambda q: ANSWER, max_subtask_retries=0,
                         persistence=Persistence(max_minutes=5, max_replans=1))
        sup.run("Compare our competitors")
        prompts = model.planner_prompts()
        assert len(prompts) == 3 and "OPERATOR ANSWERS" in prompts[2] and ANSWER in prompts[2]
        assert ASK_MARK not in prompts[2]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_supervisor.py -q`
Expected: FAIL (`TypeError: Supervisor.__init__() got an unexpected keyword argument 'ask_user'`).

- [ ] **Step 3: Implement**

All in `agentx_dev/Supervisor.py`.

1. Imports: next to the `from agentx_dev.SubAgents import (...)` block add
   ```python
   from agentx_dev.Operator import (
       NO_ANSWER_PLAN_NOTE, OperatorChannel, ask_instruction, attach_ask_user,
       parse_ask_request, validate_ask_user,
   )
   ```
   (If the file imports with the relative form, e.g. `from .SubAgents`, follow that form. Check line 36's style first.)

2. `SupervisorResult`: after the `spawned` field add
   ```python
       # Questions put to the operator (3.6): source, question, answered, reason, deduped.
       # Answer text is not kept here (it may be sensitive). Empty when ask_user is off.
       asked: List[Dict[str, Any]] = Field(default_factory=list)
   ```

3. `_SpawnMixin`: extend the docstring's needs list with `self._operator`, and replace `_drain_spawn_events` / add helpers:

   ```python
       def _drain_spawn_events(self):
           policy = self._spawn_run_policy
           if policy is not None:
               yield from policy.run.drain()
           yield from self._drain_operator_events()

       def _drain_operator_events(self):
           if self._operator is not None:
               yield from self._operator.drain()

       def _new_operator_channel(self) -> Optional[OperatorChannel]:
           return OperatorChannel.create(self.ask_user, max_questions=self.max_questions,
                                         timeout=self.ask_timeout, is_async=self._SPAWN_ASYNC,
                                         verbose=self.verbose)

       def _task_for_model(self, user_task: str) -> str:
           """The task as every model prompt sees it: the original plus any operator answers."""
           return self._operator.with_answers(user_task) if self._operator is not None else user_task

       def _with_answers(self, query: str) -> str:
           """``query`` with the operator's answers so far, for a dispatched step."""
           return self._operator.with_answers(query) if self._operator is not None else query

       def _asked_records(self) -> List[Dict[str, Any]]:
           return list(self._operator.records) if self._operator is not None else []
   ```

4. `Supervisor.__init__`: add parameters `ask_user: Any = None, max_questions: int = 3, ask_timeout: Optional[float] = None` at the end of the signature; add to the docstring Args:
   ```
               ask_user: (3.6) Lets the planner and every agent ask the human operator
                   for a fact the task leaves out. ``True`` uses the built-in asker
                   (notebook input box, terminal, or the controlling terminal; headless
                   means no answer). A function ``(question) -> str | None`` routes the
                   question through your own channel (a chatbot, a websocket). ``None``
                   (default) is off. No answer means the agent proceeds on a stated
                   assumption.
               max_questions: (3.6) Questions per run, shared by the planner and all
                   agents. Default 3.
               ask_timeout: (3.6) Seconds to wait for one answer. ``None`` lets the
                   channel decide (no limit in a notebook or terminal; 300 s on the
                   controlling-terminal path).
   ```
   and in the body, after `self.persistence = persistence`:
   ```python
           self.ask_user = validate_ask_user(ask_user, is_async=False)
           self.max_questions = max(0, int(max_questions))
           self.ask_timeout = ask_timeout
           self._operator: Optional[OperatorChannel] = None
   ```

5. `Supervisor._plan_once` — replace with (keeping the existing filtering tail unchanged):
   ```python
       def _plan_once(self, user_task: str, repair_note: str = "", ask_allowed: bool = False) -> List[dict]:
           offer = ask_allowed and self._operator is not None and self._operator.remaining() > 0
           base_prompt = SUPERVISOR_PLAN_PROMPT.format(
               agent_catalog=self._build_agent_catalog(),
               user_task=self._task_for_model(user_task),
               max_subtasks=self.max_subtasks,
           )
           prompt = base_prompt
           if self.spawn_config.enabled:
               prompt = prompt + spawn_instruction(self._spawn_policy())
           if offer:
               prompt = prompt + ask_instruction(self._operator.remaining())
           if repair_note:
               prompt = prompt + repair_note
           messages = [{"role": "user", "content": prompt}]
           response = self._call_model(messages)

           cleaned = _strip_code_fences(response)

           try:
               parsed = json.loads(cleaned)
           except json.JSONDecodeError:
               return []

           if offer and isinstance(parsed, dict) and not (parsed.get("plan") or []):
               questions = parse_ask_request(parsed.get("ask"), self._operator.remaining())
               if questions:
                   for q in questions:
                       self._operator.ask(q["question"], q["why"], "planner")
                   note = "" if self._operator.has_answers() else NO_ANSWER_PLAN_NOTE
                   # Plan again on the task plus the answers; no ask option this time.
                   return self._plan_once(user_task, repair_note=repair_note + note)

           plan = parsed.get("plan", []) or []
           ... (unchanged from here)
   ```
   `_plan`: change the first line to
   ```python
           ask_kw = {"ask_allowed": True} if self._operator is not None else {}
           plan = self._plan_once(user_task, **ask_kw)
   ```
   (The kwarg is only passed when asking is on, so a test that stubs `_plan_once` with the old signature keeps working.)

6. `_synthesize` (sync): `user_task=self._task_for_model(user_task),` in the `.format(...)` call.

7. `_run_plan` (sync), right after the `if dag_mode: ... else: dispatched_query = _build_augmented_query(sub_query, subtask_results)` block and before `yield {"type": "dispatch", ...}`:
   ```python
               dispatched_query = self._with_answers(dispatched_query)
   ```

8. `stream()`:
   - After `self._spawn_run_policy.budget = budget` add:
     ```python
           self._operator = self._new_operator_channel()
           self._spawn_run_policy.operator = self._operator
           if self._operator is not None:
               self._operator.budget = budget            # waiting on the operator pauses the deadline
     ```
   - Update the `Event shapes` docstring with the two new events:
     ```
             - {"type": "question",       "source": "planner" | <agent name>, "question": str, "context": str}
             - {"type": "answer",         "source": str, "answered": bool, "reason": None | str}
     ```
   - In the `except (CostBudgetExceeded, BudgetExpired)` branch, after `stopped = _stopped_before_planning(user_task, e)` add `stopped.asked = self._asked_records()`.
   - Immediately after that try/except (before `if not plan:`) add `yield from self._drain_operator_events()`.
   - In the "no plan" `SupervisorResult(...)` add `asked=self._asked_records(),`.
   - Replace the `with _per_run_agents(self), attach_delegation(...), apply_persistence(...)` header with:
     ```python
           with _per_run_agents(self), \
                   attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                   attach_ask_user({n: s.runner for n, s in self.agents.items()}, self._operator), \
                   apply_persistence([s.runner for s in self.agents.values()], self.persistence):
     ```
   - Before `yield {"type": "synthesize_start"}` add `yield from self._drain_operator_events()`.
   - In the final `SupervisorResult(...)` add `asked=self._asked_records(),`.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_operator_supervisor.py -q`, then `python -m pytest -q`
Expected: new tests pass; full suite green (767 + new). If an existing test fails because it stubs `_synthesize` or `_plan_once` with a fixed signature, fix the stub only if the failure is the signature; do not change production behavior.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_operator_supervisor.py
git commit -m "feat(supervisor): ask_user lets the planner and agents ask the operator (sync)"
```

---

### Task 6: Wire the AsyncSupervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py` (`AsyncSupervisor.__init__` line 1631; `_plan_once` line 1741; `_plan` line 1786; `_synthesize` line 1825; `_run_subtask` ~line 1872; `astream` line 2172)
- Test: `tests/test_operator_supervisor_async.py`

**Interfaces:**
- Consumes: everything Task 5 produced, plus `OperatorChannel.aask`.
- Produces: `AsyncSupervisor(..., ask_user=None, max_questions=3, ask_timeout=None)` with the same behavior as the sync class; async callbacks and sync callbacks both accepted.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_supervisor_async.py`:

```python
"""AsyncSupervisor + ask_user: same behaviour as the sync class, with async and sync callbacks."""

import asyncio
import json

import pytest

from agentx_dev import AgentType, AsyncAgentRunner, AsyncSupervisor, Persistence
from agentx_dev.SubAgents import SpawnConfig
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import ScriptedRunner, new_agent_step, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?", "why": "none named"}]})
ANSWER = "Notion, Obsidian, Coda"
ASK_MARK = "ASKING THE OPERATOR"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


async def collect(sup, task="task"):
    return [e async for e in sup.astream(task)]


def asking_worker(question="Which file?"):
    turns = []

    def script(messages):
        turns.append(1)
        if len(turns) == 1:
            return make_react_response("ask_user", {"question": question})
        return make_final("worker used: " + str(messages[-1]))
    return AsyncAgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)


class TestPlannerAsks:
    def test_an_async_callback_answers_the_planner(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "go"))], synth="Compared.")
        worker = ScriptedRunner()
        asked = []

        async def ask(q):
            asked.append(q)
            return ANSWER
        result = run(supervisor(model, worker, ask_user=ask), "Compare our competitors")
        first, second = model.planner_prompts()
        assert ASK_MARK in first and "OPERATOR ANSWERS" in second and ANSWER in second
        assert asked == ["Which three competitors should I compare?"]
        assert result.query == "Compare our competitors" and result.content == "Compared."
        assert result.asked[0]["source"] == "planner" and result.asked[0]["answered"] is True
        assert "OPERATOR ANSWERS" in worker.calls[0][0]

    def test_a_plain_function_works_too(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = run(supervisor(model, ask_user=lambda q: ANSWER))
        assert result.asked[0]["answered"] is True

    def test_no_answer_plans_on_assumptions(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = run(supervisor(model, ask_user=lambda q: None))
        assert "No operator answered" in model.planner_prompts()[1] and result.outcome == "done"

    def test_events_show_the_question_and_the_answer_before_the_plan(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        events = asyncio.run(collect(supervisor(model, ask_user=lambda q: ANSWER)))
        types = [e["type"] for e in events]
        assert types.index("question") < types.index("answer") < types.index("plan")

    def test_unset_changes_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = run(supervisor(model))
        assert ASK_MARK not in model.planner_prompts()[0] and result.asked == []

    def test_a_plan_wins_over_an_ask_and_zero_questions_hides_the_option(self):
        both = json.dumps({"plan": [step("s1", "worker")], "ask": [{"question": "ignored?"}]})
        asked = []
        result = run(supervisor(router(plans=[both]), ask_user=lambda q: asked.append(q) or "x"))
        assert asked == [] and result.asked == []
        model = router(plans=[plan_json(step("s1", "worker"))])
        run(supervisor(model, ask_user=lambda q: "x", max_questions=0))
        assert ASK_MARK not in model.planner_prompts()[0]

    def test_the_constructor_validates_ask_user(self):
        with pytest.raises(TypeError):
            supervisor(router(), ask_user="yes")


class TestAgentsAskMidRun:
    def test_an_async_specialist_asks_and_is_restored(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = asking_worker()
        asked = []
        result = run(supervisor(model, worker, ask_user=lambda q: asked.append(q) or "report.md"))
        assert asked == ["Which file?"] and "report.md" in result.subtasks[0].content
        assert result.asked[0]["source"] == "worker"
        assert not worker.registry.has("ask_user")

    def test_a_sync_specialist_under_the_async_supervisor_can_ask_too(self):
        from agentx_dev import AgentRunner
        turns = []

        def script(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which file?"})
                    if len(turns) == 1 else make_final("sync worker done"))
        sync_worker = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = run(supervisor(model, sync_worker, ask_user=lambda q: "report.md"))
        assert result.asked[0]["answered"] is True and result.subtasks[0].content == "sync worker done"

    def test_a_planner_defined_helper_can_ask(self):
        turns = []

        def sub(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which three competitors?"})
                    if len(turns) == 1 else make_final("helper done"))
        model = router(plans=[plan_json(new_agent_step("s1", "analyst", "You analyse.", tools=["web"]))], sub=sub)
        result = run(supervisor(model, spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                                ask_user=lambda q: ANSWER))
        assert result.asked[0]["source"] == "analyst" and result.subtasks[0].content == "helper done"

    def test_two_parallel_helpers_asking_the_same_thing_ask_the_human_once(self):
        model = router(plans=[plan_json(step("a", "w1"), step("b", "w2"))])
        asked = []
        sup = AsyncSupervisor(model=model, verbose=False, ask_user=lambda q: asked.append(q) or "report.md",
                              agents={"w1": ("one", asking_worker()), "w2": ("two", asking_worker("which file?"))})
        result = run(sup)
        assert len(asked) == 1
        assert sorted(r["deduped"] for r in result.asked) == [False, True]
        assert all("report.md" in s.content for s in result.subtasks)

    def test_a_later_step_sees_an_earlier_steps_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"), step("s2", "scribe", deps=["s1"]))])
        scribe = ScriptedRunner()
        sup = AsyncSupervisor(model=model, verbose=False, ask_user=lambda q: "report.md",
                              agents={"worker": ("asks", asking_worker()), "scribe": ("writes", scribe)})
        run(sup)
        assert "OPERATOR ANSWERS" in scribe.calls[0][0] and "report.md" in scribe.calls[0][0]


class TestPersistent:
    def test_the_run_budget_is_wired_to_the_channel(self):
        from agentx_dev.Runner.Persistence import PausableClock
        model = router(plans=[plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: "x", persistence=Persistence(max_minutes=5))
        run(sup)
        assert isinstance(sup._operator.budget._clock, PausableClock)

    def test_recovery_planning_gets_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        sup = supervisor(model, worker, ask_user=lambda q: ANSWER, max_subtask_retries=0,
                         persistence=Persistence(max_minutes=5, max_replans=1))
        run(sup, "Compare our competitors")
        prompts = model.planner_prompts()
        assert len(prompts) == 3 and "OPERATOR ANSWERS" in prompts[2] and ASK_MARK not in prompts[2]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_supervisor_async.py -q`
Expected: FAIL (`TypeError: AsyncSupervisor.__init__() got an unexpected keyword argument 'ask_user'`).

- [ ] **Step 3: Implement**

All in `agentx_dev/Supervisor.py`, mirroring Task 5 (the mixin helpers already exist):

1. `AsyncSupervisor.__init__`: add `ask_user: Any = None, max_questions: int = 3, ask_timeout: Optional[float] = None` at the end of the signature; add the same three docstring entries as Task 5 (say "a function `(question) -> str | None`, sync or `async`; a sync one runs in a worker thread"); in the body after `self.persistence = persistence`:
   ```python
           self.ask_user = validate_ask_user(ask_user, is_async=True)
           self.max_questions = max(0, int(max_questions))
           self.ask_timeout = ask_timeout
           self._operator: Optional[OperatorChannel] = None
   ```
2. `AsyncSupervisor._plan_once`: same changes as the sync version, with `ask_allowed: bool = False`, `offer` computed the same way, `user_task=self._task_for_model(user_task)` in the format call, `ask_instruction(...)` appended after the spawn instruction, and the ask branch awaiting:
   ```python
           if offer and isinstance(parsed, dict) and not (parsed.get("plan") or []):
               questions = parse_ask_request(parsed.get("ask"), self._operator.remaining())
               if questions:
                   for q in questions:
                       await self._operator.aask(q["question"], q["why"], "planner")
                   note = "" if self._operator.has_answers() else NO_ANSWER_PLAN_NOTE
                   return await self._plan_once(user_task, repair_note=repair_note + note)
   ```
   (placed right after the `json.loads` try/except, before `plan = parsed.get("plan", []) or []`).
3. `AsyncSupervisor._plan`: first line becomes
   ```python
           ask_kw = {"ask_allowed": True} if self._operator is not None else {}
           plan = await self._plan_once(user_task, **ask_kw)
   ```
4. `AsyncSupervisor._synthesize`: `user_task=self._task_for_model(user_task),`.
5. `_run_subtask`: change the `dispatched_query = (...)` expression to
   ```python
           dispatched_query = self._with_answers(
               _build_augmented_query(sub_query, prior_results)
               if prior_results else sub_query
           )
   ```
6. `astream()`:
   - After `self._spawn_run_policy.budget = budget` add the same four lines as the sync `stream()` (create the channel, set `policy.operator`, set `channel.budget`).
   - In the `except (CostBudgetExceeded, BudgetExpired)` branch add `stopped.asked = self._asked_records()` after `stopped = _stopped_before_planning(...)`.
   - After the try/except, before `if not plan:` add:
     ```python
           for ev in self._drain_operator_events():
               yield ev
     ```
   - Add `asked=self._asked_records(),` to the no-plan result and to the final `SupervisorResult(...)`.
   - Add `attach_ask_user({n: s.runner for n, s in self.agents.items()}, self._operator),` to the `with` header between `attach_delegation(...)` and `apply_persistence(...)` (same shape as Task 5).
   - Before `yield {"type": "synthesize_start"}` add the same two-line drain loop.
   - The async `_run_plan` already drains spawn events with `for ev in self._drain_spawn_events(): yield ev` after each result and spawn; `_drain_spawn_events` now includes operator events, so no further edit there.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_operator_supervisor_async.py -q`, then `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_operator_supervisor_async.py
git commit -m "feat(supervisor): ask_user for AsyncSupervisor, with async and sync callbacks"
```

---

### Task 7: Documentation, demo, changelog, spec amendments

**Files:**
- Modify: `docs/guides/sub-agents.md`, `docs/cookbook/patterns.md`, `docs/cookbook/faq.md`, `docs/cookbook/troubleshooting.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`, `CHANGELOG.md`, `examples/subagents_demo.py`, `examples/mcp_github_triage_demo.py`, `docs/superpowers/specs/2026-10-05-ask-the-operator-design.md`
- Regenerate: `host/data.js` via `python host/build_data.py` (check `git status` shows only intended files; `host/app.js` changes only if the script changes it)

**Interfaces:**
- Consumes: the finished feature. Produces: docs that match it.

- [ ] **Step 1: Sub-agents guide** — in `docs/guides/sub-agents.md`:
  - After the quick-start paragraph that says "`AsyncSupervisor` takes the same `spawn_config=`", add: "The task above names no competitors. Add `ask_user=True` and the Supervisor asks you for them (see [Asking the operator](#asking-the-operator))."
  - Add a section `## Asking the operator` before `## Watching it`, with this content (adapt headings to the file's style):

    ````markdown
    ## Asking the operator

    "Compare the pricing pages of our three competitors" never names the competitors. With `ask_user` set, the Supervisor can ask you instead of guessing.

    ```python
    supervisor = Supervisor(model=model, agents={"explorer": ("Reads files in ./workspace", explorer)},
                            spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                            ask_user=True)
    ```

    - **The planner asks first.** When the task leaves out a fact it cannot assume, it replies with questions instead of a plan. The framework asks them (up to `max_questions`, default 3), adds your answers to the task, and plans again. There is no extra model call when it plans straight away.
    - **Every agent can ask mid-run.** Specialists, planner-defined helpers and `delegate` helpers get an `ask_user(question, context="")` tool for the run (removed afterwards, so your runners are untouched). The answer goes to the agent as `[operator] ...` and to every step dispatched later.
    - **One question budget per run**, shared by all of them. An identical question is asked once; a repeat gets the first answer.
    - **No answer is not a failure.** If nobody can answer (headless, your function raised, it timed out, the budget is spent) the agent is told to proceed on a stated assumption and say what it assumed.

    `ask_user` takes:

    | Value | Meaning |
    |---|---|
    | `None` / `False` (default) | Off. Nothing changes. Use this for a chatbot or backend unless you wire a channel. |
    | `True` | The built-in asker. In a notebook (Jupyter, VS Code, Colab) it shows the input box under the cell; in a terminal it uses `input()`; when stdin is not a terminal it opens the controlling terminal (`CONIN$` / `/dev/tty`, 300 s default timeout); headless it returns no answer at once. |
    | a function `(question) -> str \| None` | Your own channel: a chat UI, a websocket, a queue. `AsyncSupervisor` also accepts an `async` function; a plain one runs in a worker thread. |

    ```python
    def ask_via_chat(question: str) -> str | None:
        session.send(question)                       # your channel
        return session.wait_for_reply(timeout=120)   # None = no answer

    Supervisor(model=model, agents=agents, ask_user=ask_via_chat, max_questions=3, ask_timeout=120)
    ```

    `ask_timeout` bounds the wait for one answer (a function that does not return in time counts as no answer). It is not enforced on the notebook input box, where a person is looking at the box and the Interrupt button stops the run. In persistent mode, time spent waiting on you does not count against `max_minutes`.

    What you can see: `question` and `answer` stream events (after the answer; your function is the live channel for a UI), `[ask]` lines with `verbose=True`, and `result.asked` (source, question, answered, reason, deduped; the answer text is not kept). Treat answers as facts: if your function forwards raw end-user text, treat it as untrusted input to the run, like the task itself.
    ````
  - In "Watching it", add rows for `question` and `answer` events.

- [ ] **Step 2: Cookbook** — read pattern 31 in `docs/cookbook/patterns.md` and mirror its heading and structure for a new `## 32. ...` "Let the Supervisor ask you for missing facts (`ask_user`)": the problem (the competitors example and the hang in Jupyter), a notebook/terminal example with `ask_user=True`, a chatbot callback example (`ask_user=my_function`), a two-line note on `max_questions`/`ask_timeout`, and "when the planner still does not ask: tell the task 'if anything is missing, ask the operator first'". Add one sentence to pattern 31 pointing to 32 as the built-in way ("pattern 31 still works when you want a human step the planner must plan"). Update any index or count of patterns that mentions 31. In `docs/cookbook/faq.md` add: "How do I let the Supervisor ask me a question?" and "Can a chatbot or backend use `ask_user`?". In `docs/cookbook/troubleshooting.md` add "My run hangs forever on a question in Jupyter" (cause: `CONIN$`/`/dev/tty` is the kernel's console, not the notebook; fix: `ask_user=True` or `ask_human_tool()` from `agentx_dev`, which use the notebook's input box) and "`ask_user=True` on a server never asks" (headless means no answer by design; pass a function).

- [ ] **Step 3: Reference and overview** — `docs/reference/api-summary.md`: add `ask_user`, `max_questions`, `ask_timeout` to the Supervisor/AsyncSupervisor rows, `SupervisorResult.asked`, the `question`/`answer` events, and `ask_human_tool`. `docs/advanced/supervisor.md` and `README.md`: one short paragraph or bullet each linking to the guide section.

- [ ] **Step 4: CHANGELOG** — in the `[3.6.0]` "Added" list add: "**Ask the operator**: `Supervisor(ask_user=True | function)` lets the planner ask for facts the task leaves out and gives every agent an `ask_user` tool. `True` uses a built-in asker that works in notebooks (the input box), terminals and IDE consoles and never blocks a headless process; a function routes questions through your own channel (chatbot, backend). `max_questions`, `ask_timeout`, `SupervisorResult.asked`, `question`/`answer` events, `agentx_dev.ask_human_tool()`; waiting on the operator pauses the persistent deadline." Do not change the date or version.

- [ ] **Step 5: Demo** — `examples/subagents_demo.py`: add `ASK_TASK = "Compare the pricing pages of our three competitors."`; `build_supervisor(model, persistent=False, ask=False)` passes `ask_user=True` when `ask`; in `main`, `ask = "--ask" in argv`, use `ASK_TASK` as the default task when `ask` and no task was given, update the module docstring ("python examples/subagents_demo.py --ask   # the planner asks you for the competitors") and print `result.asked` entries (source, question, answered) after the helpers list. `examples/mcp_github_triage_demo.py`: delete the local `ask_human_tool` definition and the `_AskHumanArgs` class it alone uses, add `from agentx_dev import ask_human_tool`, keep the call site `ask_human_tool()` (pass `prompt_prefix="[triager]"` to keep the prefix); keep the file importable (`python -c "import ast,sys; ast.parse(open('examples/mcp_github_triage_demo.py').read())"`).

- [ ] **Step 6: Amend the spec** to match the rulings: in `docs/superpowers/specs/2026-10-05-ask-the-operator-design.md` update 3.2 (registered specialists rely on the tool description; helpers get the addendum line), 3.3 (the notebook path ignores `ask_timeout`), 3.1/4.1 (the callable receives the question with `(context: ...)` appended), and 4.1 (the budget counts questions put to the operator, including unanswered ones). Keep it one short commit-worthy edit.

- [ ] **Step 7: Regenerate and verify**

Run: `python host/build_data.py` and `python -m pytest -q`
Run the docs syntax check the CI job uses (see `.github/workflows/test.yml`, the "docs syntax" job) on the changed docs, and confirm the new Python blocks parse.
Expected: suite green; syntax check passes; `git status` lists only the files named above plus `host/data.js`.

- [ ] **Step 8: Commit**

```bash
git add docs README.md CHANGELOG.md examples host
git commit -m "docs: ask the operator (guide, cookbook 32, FAQ, troubleshooting, demo --ask, changelog)"
```

- [ ] **Step 9: Manual check for the user (not automatable here)** — report in the final hand-back that the user must run this in a real Jupyter notebook and confirm the input box appears and the run completes:

```python
from agentx_dev import GPT, Supervisor, SpawnConfig
sup = Supervisor(model=GPT(model="gpt-4o-mini"), agents={}, ask_user=True,
                 spawn_config=SpawnConfig(enabled=True, capabilities={"web"}))
print(sup.run("Compare the pricing pages of our three competitors").content)
```
