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
