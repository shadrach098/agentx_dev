"""Full-screen mode (``agentx --fullscreen``, ``"fullscreen": true`` in ~/.agentx/settings.json).

agentx takes the whole window, the way vim or htop do, and draws fixed areas:

    the header (the welcome box; one line on a short window)          fixed
    the conversation                                                  scrolls (wheel, PgUp / PgDn, Ctrl+End)
    the live block (team, look-around tree, spinner) or a question    fixed
    a rule, the input line, the status line                           fixed

The app itself runs unchanged on a worker thread. What it prints through its rich ``Console`` lands in the
conversation (the console writes into ``_Sink``); what it asks (``read``, ``read_short``, ``select``) is answered in
the fixed input area, so a question never scrolls away. The spinner is drawn here instead of by rich's ``Live``
(``Progress.drawn_by``). Ctrl+C at a question answers it the way it did before (``KeyboardInterrupt`` at the input,
cancel in a menu); Ctrl+C while the agent works raises ``KeyboardInterrupt`` in the worker, as it did on the main
thread. A message typed while the agent works is kept and sent when it next asks for one.

On exit the screen goes back to what it was, and the whole conversation is written to the terminal once, so it is
in the scrollback as usual."""

from __future__ import annotations

import asyncio
import ctypes
import io
import shutil
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from agentx_dev.cli.ui import (ACCENT, CANCELLED, input_style, make_completer, make_lexer, plain, safe_toolbar)

MAX_LINES = 20_000                  # conversation rows kept on screen (the transcript keeps everything)
MAX_TRANSCRIPT_CHARS = 8_000_000    # written to the terminal on exit
COMPACT_BELOW_ROWS = 30             # on a shorter window the header is one line
INPUT_MAX_ROWS = 8
WHEEL_ROWS = 3

Fragments = List[Tuple[str, str]]


class _Sink(io.TextIOBase):
    """The file the app's console writes to: every write goes to the conversation."""

    encoding = "utf-8"

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__()
        self._write = write

    def write(self, text: str) -> int:          # type: ignore[override]
        self._write(str(text))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def writable(self) -> bool:
        return True


def ansi_fragments(text: str) -> Fragments:
    """One line of rich's output (ANSI colours) as prompt_toolkit fragments; plain text if it cannot be read."""
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    try:
        return list(to_formatted_text(ANSI(text)))
    except Exception:
        return [("", plain(text))]


def render_ansi(renderable: Any, width: int) -> List[str]:
    """A rich renderable as lines of ANSI text, ``width`` columns wide."""
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, color_system="truecolor", width=max(20, width),
                      legacy_windows=False)
    console.print(renderable)
    return buffer.getvalue().rstrip("\n").split("\n")


def raise_in(thread: Optional[threading.Thread], error: type = KeyboardInterrupt) -> bool:
    """Raise ``error`` in ``thread`` at its next Python instruction (how Ctrl+C reaches the worker)."""
    if thread is None or not thread.is_alive() or thread.ident is None:
        return False
    try:
        done = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(error))
    except Exception:
        return False
    return done == 1


class FullScreen:
    """The full-screen front end. ``console``, ``read``, ``read_short`` and ``select`` are what the app is given
    instead of a terminal console and ``PromptInput``; ``run(work)`` runs ``work`` (the app) on a worker thread while
    this thread draws the screen, and returns what ``work`` returned."""

    def __init__(self, *, history_file: Path, commands: Sequence[str], toolbar: Callable[[], Any],
                 cycle_mode: Callable[[], object], descriptions: Optional[Mapping[str, str]] = None,
                 extra_commands: Optional[Callable[[], Mapping[str, str]]] = None) -> None:
        self._lock = threading.RLock()
        self._lines: List[Fragments] = []
        self._partial = ""
        self._transcript: List[str] = []
        self._transcript_size = 0
        self._top: Optional[int] = None          # the first conversation row shown; None follows the end
        self._height = 20
        size = shutil.get_terminal_size((100, 30))
        self._width = size.columns
        self._header: Optional[Tuple[Callable[[int], Any], str]] = None
        self._header_cache: Tuple[Any, Fragments] = (None, [])
        self._request: Optional[Dict[str, Any]] = None
        self._queued: Optional[str] = None
        self._worker: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self.progress: Any = None
        self._toolbar = safe_toolbar(toolbar)
        self._cycle_mode = cycle_mode
        self.console = Console(file=_Sink(self._append), force_terminal=True, color_system="truecolor",
                               width=self._width, legacy_windows=False)
        self._build(history_file, commands, descriptions, extra_commands)

    # -- what the app is given -----------------------------------------------------------------------------------

    def set_header(self, factory: Callable[[int], Any], compact: str = "") -> None:
        """The header: ``factory(width)`` builds it (the welcome box); ``compact`` is its one-line form."""
        with self._lock:
            self._header = (factory, plain(compact))
            self._header_cache = (None, [])
        self.invalidate()

    def read(self, prompt: str = "> ") -> str:
        """The next message: one typed while the agent worked, or the next one sent."""
        with self._lock:
            queued, self._queued = self._queued, None
        if queued is not None:
            self._echo_message(queued)
            return queued
        return self._wait("chat")

    def read_short(self, prompt: str = "") -> str:
        return self._wait("short", prompt=plain(prompt))

    def select(self, options: Sequence[str], default: int = 0, hotkeys: Optional[Mapping[str, int]] = None):
        labels = [plain(option) for option in options]
        if not labels:
            return None
        return self._wait("select", options=labels, position=default if 0 <= default < len(labels) else 0,
                          hotkeys={str(k): int(v) for k, v in (hotkeys or {}).items()
                                   if len(str(k)) == 1 and 0 <= int(v) < len(labels)})

    def invalidate(self) -> None:
        try:
            self.app.invalidate()
        except Exception:
            pass

    def run(self, work: Callable[[], int]) -> int:
        box: Dict[str, Any] = {}

        def worker() -> None:
            try:
                box["code"] = work()
            except BaseException as error:          # noqa: BLE001 - raised again on this thread after the screen closes
                box["error"] = error
            finally:
                self._on_ui(self._finish)

        def pre_run() -> None:
            self._loop = asyncio.get_event_loop()
            self._worker = threading.Thread(target=worker, name="agentx-work", daemon=True)
            self._worker.start()

        try:
            self.app.run(pre_run=pre_run)
        finally:
            with self._lock:
                self._closed = True
                request, self._request = self._request, None
            if request is not None and not request["future"].done():
                request["future"].set_exception(EOFError())
            if self._worker is not None:
                self._worker.join(5)
            self._write_transcript()
        if "error" in box:
            raise box["error"]
        return int(box.get("code") or 0)

    # -- the conversation --------------------------------------------------------------------------------------

    def _append(self, text: str) -> None:
        with self._lock:
            if self._transcript_size < MAX_TRANSCRIPT_CHARS:
                self._transcript.append(text)
                self._transcript_size += len(text)
            data = (self._partial + text).replace("\r\n", "\n")
            *done, self._partial = data.split("\n")
            for line in done:
                self._lines.append(ansi_fragments(line.replace("\r", "")))
            extra = len(self._lines) - MAX_LINES
            if extra > 0:
                del self._lines[:extra]
                if self._top is not None:
                    self._top = max(0, self._top - extra)
        self.invalidate()

    def visible(self, height: int, width: Optional[int] = None) -> List[Fragments]:
        """The conversation rows to show in ``height`` rows: the end, or where the user scrolled to."""
        with self._lock:
            if width and width != self._width:
                self._width = width
                self.console.width = width
            self._height = max(1, height)
            rows = self._lines + ([ansi_fragments(self._partial)] if self._partial else [])
            last_top = max(0, len(rows) - self._height)
            top = last_top if self._top is None else min(self._top, last_top)
            return rows[top: top + self._height]

    def scroll_by(self, rows: int) -> None:
        with self._lock:
            total = len(self._lines) + (1 if self._partial else 0)
            last_top = max(0, total - self._height)
            current = last_top if self._top is None else self._top
            new = max(0, min(last_top, current + rows))
            self._top = None if new >= last_top else new
        self.invalidate()

    def follow(self) -> None:
        with self._lock:
            self._top = None
        self.invalidate()

    def scrolled_up(self) -> bool:
        return self._top is not None

    def _write_transcript(self) -> None:
        try:
            sys.stdout.write("".join(self._transcript))
            sys.stdout.flush()
        except Exception:
            pass

    # -- the fixed parts ---------------------------------------------------------------------------------------

    def _rows(self) -> int:
        try:
            return self.app.output.get_size().rows
        except Exception:
            return shutil.get_terminal_size((100, 30)).lines

    def header_fragments(self) -> Fragments:
        with self._lock:
            header = self._header
            if header is None:
                return []
            compact = self._rows() < COMPACT_BELOW_ROWS
            key = (self._width, compact)
            if self._header_cache[0] == key:
                return self._header_cache[1]
            factory, short = header
            if compact:
                lines = render_ansi(Text(short, style=f"bold {ACCENT}"), self._width)
            else:
                try:
                    lines = render_ansi(factory(self._width), self._width)
                except Exception:
                    lines = render_ansi(Text(short), self._width)
            fragments: Fragments = []
            for number, line in enumerate(lines):
                if number:
                    fragments.append(("", "\n"))
                fragments.extend(ansi_fragments(line))
            self._header_cache = (key, fragments)
            return fragments

    def header_height(self) -> int:
        fragments = self.header_fragments()
        return sum(text.count("\n") for _, text in fragments) + 1 if fragments else 0

    def live_lines(self) -> List[str]:
        """The block above the input: a question being asked, else the spinner with its block (team, tree)."""
        with self._lock:
            request = dict(self._request) if self._request else None
        if request is not None and request["kind"] == "select":
            lines = []
            for number, label in enumerate(request["options"]):
                mark = ">" if number == request["position"] else " "
                text = f"{mark} {number + 1}. {label}"
                lines.append(f"\x1b[1;36m{text}\x1b[0m" if number == request["position"] else text)
            lines.append("\x1b[90mUp / Down to move, Enter to confirm, Esc to cancel\x1b[0m")
            return lines
        if request is not None and request["kind"] == "short":
            return [f"\x1b[1;33m{request['prompt']}\x1b[0m"] if request["prompt"] else []
        progress = self.progress
        renderable = None
        if progress is not None:
            try:
                renderable = progress.renderable()
            except Exception:
                renderable = None
        lines = render_ansi(renderable, self._width) if renderable is not None else []
        if self.scrolled_up():
            lines.append("\x1b[90m(scrolled up: Ctrl+End or PgDn back to the end)\x1b[0m")
        return lines

    def live_fragments(self) -> Fragments:
        fragments: Fragments = []
        for number, line in enumerate(self.live_lines()):
            if number:
                fragments.append(("", "\n"))
            fragments.extend(ansi_fragments(line))
        return fragments

    def live_height(self) -> int:
        return len(self.live_lines())

    # -- questions -----------------------------------------------------------------------------------------------

    def _wait(self, kind: str, **data: Any) -> Any:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise EOFError()
            self._request = {"kind": kind, "future": future, **data}
        self._on_ui(self._focus_for_request)
        return future.result()

    def _answer(self, value: Any = None, error: Optional[BaseException] = None) -> None:
        with self._lock:
            request, self._request = self._request, None
        if request is None or request["future"].done():
            return
        if error is not None:
            request["future"].set_exception(error)
        else:
            request["future"].set_result(value)
        self._focus_for_request()

    def kind(self) -> str:
        with self._lock:
            return self._request["kind"] if self._request else ""

    def _echo_message(self, text: str) -> None:
        self.console.print(Rule(style="#5a5a62"))
        line = Text("❯ ", style=f"bold {ACCENT}")
        line.append(plain(text, keep_newlines=True))
        self.console.print(line)

    def submit_chat(self) -> None:
        text = self.chat_buffer.text
        if not text.strip() and self.kind() != "chat":
            return
        try:
            self.chat_buffer.append_to_history()
        except Exception:
            pass
        self.chat_buffer.reset()
        self.follow()
        if self.kind() == "chat":
            self._echo_message(text)
            self._answer(text)
        else:                                       # the agent is working: sent when it next asks for a message
            with self._lock:
                self._queued = text if self._queued is None else self._queued + "\n" + text
            self.console.print(Text("(kept: sent when the agent is done)", style="dim"))

    def submit_short(self) -> None:
        text = self.short_buffer.text
        self.short_buffer.reset()
        with self._lock:
            prompt = self._request.get("prompt", "") if self._request else ""
        line = Text(prompt, style="bold yellow")
        line.append(text)
        self.console.print(line)
        self.follow()
        self._answer(text)

    def move(self, step: int) -> None:
        with self._lock:
            if self._request and self._request["kind"] == "select":
                count = len(self._request["options"])
                self._request["position"] = (self._request["position"] + step) % count
        self.invalidate()

    def choose(self, index: Optional[int] = None) -> None:
        with self._lock:
            request = self._request
            if not request or request["kind"] != "select":
                return
            index = request["position"] if index is None else index
            label = request["options"][index]
        self.console.print(Text(f"> {index + 1}. {label}", style="bold cyan"))
        self._answer(index)

    def ctrl_c(self) -> None:
        kind = self.kind()
        if kind == "select":
            self.console.print(Text("(cancelled)", style="dim"))
            self._answer(CANCELLED)
        elif kind in ("chat", "short"):
            (self.chat_buffer if kind == "chat" else self.short_buffer).reset()
            self._answer(error=KeyboardInterrupt())
        else:
            raise_in(self._worker)

    def ctrl_d(self) -> None:
        if self.kind() == "chat" and not self.chat_buffer.text:
            self._answer(error=EOFError())
        elif self.kind() == "select":
            self._answer(CANCELLED)

    # -- prompt_toolkit ------------------------------------------------------------------------------------------

    def _on_ui(self, function: Callable[[], Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(function)
        except RuntimeError:                         # the screen has closed
            pass

    def _finish(self) -> None:
        try:
            if self.app.is_running:
                self.app.exit()
        except Exception:
            pass

    def _focus_for_request(self) -> None:
        try:
            if self.kind() == "short":
                self.app.layout.focus(self.short_buffer)
            else:
                self.app.layout.focus(self.chat_buffer)
        except Exception:
            pass
        self.invalidate()

    def _build(self, history_file: Path, commands: Sequence[str], descriptions: Optional[Mapping[str, str]],
               extra_commands: Optional[Callable[[], Mapping[str, str]]]) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition, has_focus
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (ConditionalContainer, Float, FloatContainer, HSplit, Window)
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.mouse_events import MouseEventType

        screen = self
        try:
            history: Any = FileHistory(str(history_file))
        except Exception:
            history = InMemoryHistory()
        self.chat_buffer = Buffer(multiline=True, history=history,
                                  completer=make_completer(commands, descriptions, extra_commands),
                                  complete_while_typing=True)
        self.short_buffer = Buffer(multiline=False)

        class Conversation(UIControl):
            def create_content(self, width: int, height: int) -> UIContent:
                rows = screen.visible(height, width)
                return UIContent(get_line=lambda number: rows[number] if number < len(rows) else [],
                                 line_count=len(rows), show_cursor=False)

            def mouse_handler(self, mouse_event):
                if mouse_event.event_type == MouseEventType.SCROLL_UP:
                    screen.scroll_by(-WHEEL_ROWS)
                    return None
                if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    screen.scroll_by(WHEEL_ROWS)
                    return None
                return NotImplemented

            def is_focusable(self) -> bool:
                return False

        asking_short = Condition(lambda: screen.kind() == "short")
        selecting = Condition(lambda: screen.kind() == "select")
        chatting = has_focus(self.chat_buffer) & ~selecting & ~asking_short

        header = Window(FormattedTextControl(self.header_fragments), height=lambda: Dimension.exact(self.header_height()),
                        dont_extend_height=True)
        conversation = Window(Conversation(), wrap_lines=False)
        live = Window(FormattedTextControl(self.live_fragments), height=lambda: Dimension.exact(self.live_height()),
                      dont_extend_height=True)
        rule = Window(char="─", height=1, style="class:rule")
        chat_input = ConditionalContainer(
            Window(BufferControl(buffer=self.chat_buffer, lexer=make_lexer(commands, extra_commands),
                                 input_processors=[BeforeInput([("class:prompt", "❯ ")])]),
                   height=Dimension(min=1, max=INPUT_MAX_ROWS), wrap_lines=True, dont_extend_height=True),
            filter=~asking_short)
        short_input = ConditionalContainer(
            Window(BufferControl(buffer=self.short_buffer, input_processors=[BeforeInput([("class:prompt", "› ")])]),
                   height=1, dont_extend_height=True),
            filter=asking_short)
        footer = Window(FormattedTextControl(lambda: self._toolbar()), height=1, style="class:bottom-toolbar")
        body = HSplit([header, conversation, live, rule, chat_input, short_input, footer])
        root = FloatContainer(content=body, floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12, scroll_offset=1))])

        bindings = KeyBindings()

        @bindings.add("enter", filter=chatting)
        def _send(event):
            buffer = event.current_buffer
            state = buffer.complete_state
            if state is not None and state.current_completion is not None:
                buffer.apply_completion(state.current_completion)
                return
            screen.submit_chat()

        @bindings.add("escape", "enter", filter=chatting)
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("enter", filter=asking_short)
        def _answer(event):
            screen.submit_short()

        @bindings.add("up", filter=selecting)
        @bindings.add("k", filter=selecting)
        def _up(event):
            screen.move(-1)

        @bindings.add("down", filter=selecting)
        @bindings.add("j", filter=selecting)
        def _down(event):
            screen.move(1)

        @bindings.add("enter", filter=selecting)
        def _pick(event):
            screen.choose()

        @bindings.add("escape", filter=selecting, eager=True)
        def _cancel(event):
            screen.console.print(Text("(cancelled)", style="dim"))
            screen._answer(CANCELLED)

        @bindings.add(Keys.Any, filter=selecting)
        def _key(event):
            key = event.data
            with screen._lock:
                request = screen._request or {}
            if key in request.get("hotkeys", {}):
                screen.choose(request["hotkeys"][key])
            elif key.isdigit() and 1 <= int(key) <= min(9, len(request.get("options", []))):
                screen.choose(int(key) - 1)

        @bindings.add("s-tab")
        def _cycle(event):
            try:
                screen._cycle_mode()
            finally:
                screen.invalidate()

        @bindings.add("pageup")
        def _page_up(event):
            screen.scroll_by(-max(1, screen._height - 2))

        @bindings.add("pagedown")
        def _page_down(event):
            screen.scroll_by(max(1, screen._height - 2))

        @bindings.add("c-end")
        def _end(event):
            screen.follow()

        @bindings.add("c-c")
        def _interrupt(event):
            screen.ctrl_c()

        @bindings.add("c-d")
        def _leave(event):
            screen.ctrl_d()

        self.app = Application(layout=Layout(root, focused_element=self.chat_buffer), key_bindings=bindings,
                               style=input_style(), full_screen=True, mouse_support=True, refresh_interval=0.1)
