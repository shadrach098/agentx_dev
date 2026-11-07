"""``/remote-control`` in the terminal: the running session, also in a window on your iPad, phone or another computer.

The terminal keeps working as before and shows everything; the window (the same one as ``agentx --ui``) shows the same
conversation live and can drive it. A message, an answer or a menu choice made in the window is typed into the
terminal's own prompt or menu, so the terminal shows it as if typed there; whichever side answers a question first
wins. The window is reached through Tailscale (a link for an iPad or phone) or an SSH tunnel, never the open network
(see ``webui.remote_instructions``)."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, Optional

from agentx_dev.cli.approval import Answer
from agentx_dev.cli.ui import CANCELLED, pick_in_menu

FEED_SECONDS = 5.0                   # how long an answer from the window waits for the terminal's prompt to open
_CURSOR_MOVES = re.compile(r"\r|\x1b\[[0-9;?]*[A-La-lN-Zn-z]")   # a spinner redrawing itself, not text to show


class _Quiet(threading.local):
    on = False


class _TeeFile:
    """The terminal console's file: everything is written to the terminal; what the app prints itself (tables,
    /usage) is also sent to the window. What the screen draws (it goes to the window as events) and a spinner's
    redraws are not."""

    def __init__(self, original: Any, also: Callable[[str], None], quiet: _Quiet) -> None:
        self._original = original
        self._also = also
        self._quiet = quiet

    def write(self, text: str) -> int:
        written = self._original.write(text)
        if not self._quiet.on and text and not _CURSOR_MOVES.search(text):
            try:
                self._also(text)
            except Exception:
                pass
        return written

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, name: str) -> Any:             # isatty, fileno, encoding...: the terminal's
        return getattr(self._original, name)


class _TeeScreen:
    """The app's screen: each call goes to the terminal's screen, then to the window's (``WebScreen``)."""

    def __init__(self, terminal: Any, web: Any, quiet: _Quiet) -> None:
        object.__setattr__(self, "_terminal", terminal)
        object.__setattr__(self, "_web", web)
        object.__setattr__(self, "_quiet", quiet)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._terminal, name)
        if not callable(value):
            return value
        other = getattr(self._web, name, None)
        quiet = self._quiet

        def both(*args: Any, **kwargs: Any) -> Any:
            before, quiet.on = quiet.on, True
            try:
                result = value(*args, **kwargs)
            finally:
                quiet.on = before
            if callable(other):
                try:
                    other(*args, **kwargs)
                except Exception:                     # the window must never break the terminal
                    pass
            return result
        return both

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._terminal, name, value)
        try:
            setattr(self._web, name, value)
        except Exception:
            pass


class RemoteControl:
    """One ``/remote-control`` session for a terminal app (``app``) and its prompt (``ui.PromptInput``)."""

    def __init__(self, app: Any, prompt: Any) -> None:
        self.app = app
        self.prompt = prompt
        self.front: Any = None
        self.active = False
        self._saved: Dict[str, Any] = {}
        self._quiet = _Quiet()
        self._approval: Optional[Dict[str, Any]] = None
        self._reason: Optional[str] = None
        self.instructions = ""

    # -- on and off ----------------------------------------------------------------------------------------------

    def start(self) -> str:
        from agentx_dev.cli.team import on_main
        from agentx_dev.cli.webui import WebFront, WebScreen, remote_instructions, tailscale_address

        app = self.app
        front = WebFront(cwd=app.cwd, home=app.home, open_it=False, remote=True)
        front.app = app
        front._worker = threading.main_thread()          # the Stop button interrupts the terminal's turn
        web_screen = WebScreen(front.console, front.bus)
        web_screen.cwd = getattr(app.screen, "cwd", None)
        console = app.console
        self._saved = {"screen": app.screen, "echo": app._echo, "ask": app.gate.ask, "file": console.file}
        app.screen = _TeeScreen(app.screen, web_screen, self._quiet)
        terminal_echo = self._saved["echo"]

        def echo(text: str, outcome: str, done: bool = True) -> None:
            terminal_echo(text, outcome, done)
            front.bus.emit("echo", text=str(text)[:2000], outcome=str(outcome)[:600], done=bool(done))
        app._echo = echo
        app.gate.ask = lambda request: on_main(self.approving, request)
        console.file = _TeeFile(console.file, front._printed, self._quiet)
        self.prompt.mirror = self
        front.on_window = self._say_in_terminal          # "a window connected": shown above the open prompt
        front.tailnet = tailscale_address()
        front.start_server()
        self.front = front
        self.active = True
        self.instructions = remote_instructions(front.address(), tailnet_url=front.tailnet_address(),
                                                tailnet_problem=front.tailnet_problem)
        return self.instructions

    def _say_in_terminal(self, text: str) -> None:
        """A line in the terminal from another thread: above the open prompt (so it is not broken), else printed."""
        session = getattr(self.prompt, "_chat", None)
        application = getattr(session, "app", None)
        loop = getattr(application, "loop", None) if application is not None else None
        if application is not None and loop is not None and getattr(application, "is_running", False):
            try:
                from prompt_toolkit.application import run_in_terminal
                loop.call_soon_threadsafe(lambda: run_in_terminal(lambda: print(text)))
                return
            except Exception:
                pass
        before, self._quiet.on = self._quiet.on, True     # the terminal only: the window knows already
        try:
            self.app.console.print(text, style="dim", markup=False, highlight=False)
        finally:
            self._quiet.on = before

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        app = self.app
        self.prompt.mirror = None
        saved = self._saved
        app.screen = saved.get("screen", app.screen)
        app._echo = saved.get("echo", app._echo)
        app.gate.ask = saved.get("ask", app.gate.ask)
        if "file" in saved:
            app.console.file = saved["file"]
        if self.front is not None:
            self.front.close()                          # open windows say agentx stopped; the terminal goes on

    # -- the questions: asked in the terminal and in the window; the first answer wins ------------------------

    def _feed(self, done: threading.Event, give: Callable[[], bool]) -> None:
        """Give the window's answer to the terminal once its prompt or menu is open (it may open a moment later)."""
        def work() -> None:
            deadline = time.monotonic() + FEED_SECONDS
            while not done.is_set() and time.monotonic() < deadline:
                if give():
                    return
                time.sleep(0.05)
        threading.Thread(target=work, name="agentx-remote-answer", daemon=True).start()

    def around(self, kind: str, run: Callable[[], Any], **data: Any) -> Any:
        """Ask in the terminal (``run``) while the window shows the same question; an answer from the window is
        typed into the terminal. ``kind``: ``message`` (the next message), ``text`` (a short answer) or ``select``."""
        if not self.active or self.front is None:
            return run()
        if self._approval is not None and kind == "select":
            return run()                                  # the approval card in the window is this question
        done = threading.Event()
        if self._approval is not None and kind == "text":
            reason, self._reason = self._reason, None
            if reason is not None:                         # "No" in the window, with what to do instead
                self._feed(done, lambda: self.prompt.type_in("text", reason))
            try:
                return run()
            finally:
                done.set()
        web_kind = {"message": "message", "text": "text", "select": "select"}[kind]
        try:
            ident, future = self.front.open_request(web_kind, **({"prompt": data.get("prompt", "")} if kind == "text"
                                                                 else {"options": data.get("options", []),
                                                                       "default": data.get("default", 0)}
                                                                 if kind == "select" else {}))
        except EOFError:
            return run()

        def answered(f: Future) -> None:
            if f.cancelled() or f.exception() is not None:
                if kind == "text" and not f.cancelled():  # Stop in the window: an empty answer
                    self._feed(done, lambda: self.prompt.type_in("text", ""))
                return
            value = f.result()
            if kind == "select":
                index = value if isinstance(value, int) else CANCELLED
                self._feed(done, lambda: pick_in_menu(index))
            else:
                self._feed(done, lambda: self.prompt.type_in(kind, str(value)))
        future.add_done_callback(answered)
        try:
            result = run()
        finally:
            done.set()
            if not future.done():
                future.cancel()
            self.front.close_request(ident)
        if kind == "message" and isinstance(result, str):
            self.front.bus.emit("user", text=result[:100_000])
        return result

    def approving(self, request: Any) -> Answer:
        """The gate's question (on the main thread): the terminal's prompt and the window's card together."""
        ask = getattr(self.app, "_ask_remembering", None)
        front = self.front
        if ask is None:
            return self._saved["ask"](request)
        if not self.active or front is None:
            return ask(request)
        fields = front.approval_fields(request)
        if isinstance(fields, Answer):
            return ask(request)                           # declined unseen; the terminal says so too
        try:
            ident, future = front.open_request("approval", **fields)
        except EOFError:
            return ask(request)
        done = threading.Event()
        labels_count = 3 if fields["always"] else 2

        def answered(f: Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            value = f.result() if isinstance(f.result(), dict) else {"choice": "n"}
            choice = str(value.get("choice") or "n")
            if choice == "a" and not fields["always"]:
                choice = "y"
            index = 0 if choice == "y" else 1 if choice == "a" else labels_count - 1
            if choice == "n":
                self._reason = str(value.get("reason") or "").strip()
            self._feed(done, lambda: pick_in_menu(index))
        future.add_done_callback(answered)
        self._approval = {"id": ident}
        try:
            answer = ask(request)
        finally:
            self._approval = None
            self._reason = None
            done.set()
            if not future.done():
                future.cancel()
            outcome = ""
        try:
            choice = getattr(answer, "choice", "n")
            outcome = {"y": "Approved", "a": "Approved, and won't ask again"}.get(choice, "Declined")
        except Exception:
            pass
        front.close_request(ident, outcome)
        return answer
