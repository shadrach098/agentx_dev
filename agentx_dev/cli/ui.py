"""Terminal rendering (rich) and input (prompt_toolkit) for the agentx command.

``prompt_toolkit`` is imported inside ``make_completer`` and ``PromptInput`` only, so everything else here
works without it and can be tested without a terminal.

Everything the model or the project supplies (tool arguments, previews, file content, thoughts, titles, the
final answer) is printed as rich ``Text`` after ``plain`` has defused control characters and escape
sequences, so it can neither style nor spoof the interface. Only the final answer is rendered as Markdown
(with hyperlinks off). Nothing here may kill a turn because of the console: text the console cannot encode is
printed with backslash escapes, and any failure to read an answer is a "no"."""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from agentx_dev.cli.approval import Answer, Request, subject_of
from agentx_dev.cli.preview import Preview, preview_for

MAX_RESULT_LINES = 3
MAX_SUMMARY_CHARS = 100
MAX_QUESTION_LINES = 12
MAX_QUESTION_CHARS = 1500
MAX_HELPER_CHARS = 40

# What ``plain`` escapes: control characters (Cc), lone surrogates (Cs) and every format character (Cf: bidi
# controls, zero-width spaces, word joiners, BOM, soft hyphen, tag characters, interlinear annotation marks...)
# except the two joiners emoji sequences need (U+200C, U+200D); every space-separator (Zs) other than the plain
# space (NBSP, U+3000, U+2000-200A, U+202F, U+205F, U+1680); and the look-alike blanks that no category catches:
# line and paragraph separators, the Hangul and Braille fillers, the combining grapheme joiner.
_ESCAPED = ("Cc", "Cs", "Cf")
_UNSAFE = frozenset([0x2028, 0x2029, 0x3164, 0x115F, 0x1160, 0x034F, 0xFFA0, 0x2800])
_JOINERS = frozenset([0x200C, 0x200D])


def _escape(char: str) -> str:
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _normalised(text: str) -> str:
    return text.replace("\r\n", "\n")


def plain(text: Any, *, keep_newlines: bool = False) -> str:
    """``text`` made safe to print: control characters (ESC, BEL, CR, C1 codes, VT, FF...), lone surrogates,
    format characters and invisible fillers are replaced by a visible escape such as ``\\x1b``. Tabs stay.
    Line breaks stay when ``keep_newlines`` is true (CR LF counts as one; every other break character is
    escaped, not treated as a break) and become a visible ``\\n`` otherwise."""
    try:
        text = text if isinstance(text, str) else str(text)
    except Exception:
        return "(cannot be shown)"
    text = _normalised(text)
    out = []
    for char in text:
        code = ord(char)
        if char == "\t" or (char == "\n" and keep_newlines):
            out.append(char)
        elif char == "\n":
            out.append("\\n")
        elif code in _UNSAFE or (unicodedata.category(char) in _ESCAPED and code not in _JOINERS) \
                or (char != " " and unicodedata.category(char) == "Zs"):
            out.append(_escape(char))
        else:
            out.append(char)
    return "".join(out)


def _lines(text: str) -> List[str]:
    """Split on ``\\n`` only (after CR LF is normalised), so ``plain`` has already escaped every other break
    character instead of it silently splitting a line. One trailing newline does not add an empty line."""
    parts = _normalised(text).split("\n")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return parts


def bullet(console: Console) -> str:
    """The marker in front of a tool call; a plain star on a console that cannot print the dot."""
    return "●" if "utf" in (console.encoding or "").lower() else "*"


def _short(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    flat = " ".join(plain(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _print(console: Console, *renderables: Any) -> None:
    """``console.print`` that survives a console which cannot encode what is printed (a cp1252 or ASCII
    stdout): the output is rendered to a string and written with unencodable characters as backslash
    escapes."""
    try:
        console.print(*renderables)
        return
    except UnicodeEncodeError:
        pass
    # The failed write left what it rendered in the console's buffer. Capturing nothing returns exactly that
    # (and empties the buffer), so the line is written once: printing it again here would add it to the stale
    # copy and write it twice.
    with console.capture() as capture:
        pass
    data = capture.get()
    encoding = console.encoding or "ascii"
    try:
        safe = data.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        safe = data.encode("ascii", "backslashreplace").decode("ascii")
    console.file.write(safe)
    console.file.flush()


class Progress:
    """A line with a moving spinner and the seconds elapsed ("Running run_python... 12s") shown while the agent
    works, so a long model call or script does not look like a hang. It only draws on a real terminal (nothing
    on a pipe or a captured console) and is erased when it stops. Stop it before printing anything and while a
    prompt is waiting for an answer (``paused`` does both)."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._live = None
        self._label = "Thinking"
        self._since = 0.0
        self._spinner = None

    @property
    def enabled(self) -> bool:
        try:
            return bool(self.console.is_terminal)
        except Exception:
            return False

    def __rich__(self):
        import time
        if self._spinner is not None:
            self._spinner.update(text=Text(f"{self._label}... {int(time.monotonic() - self._since)}s", style="dim"))
        return self._spinner

    def start(self, label: str = "Thinking") -> None:
        self.stop()
        if not self.enabled:
            return
        import time
        from rich.live import Live
        from rich.spinner import Spinner
        self._label = plain(label)
        self._since = time.monotonic()
        utf = (getattr(self.console, "encoding", "") or "").lower().startswith("utf")
        self._spinner = Spinner("dots" if utf else "line", text=Text(f"{self._label}...", style="dim"))
        try:
            self._live = Live(self, console=self.console, transient=True, refresh_per_second=10)
            self._live.start()
        except Exception:
            self._live = None

    def stop(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass

    def paused(self):
        """``with progress.paused():`` stops the spinner for the block and restarts it (same label) after."""
        from contextlib import contextmanager

        @contextmanager
        def block():
            running = self._live is not None
            label = self._label
            self.stop()
            try:
                yield
            finally:
                if running:
                    self.start(label)
        return block()


class Screen:
    """Draws what the agent does. Everything is printed as ``Text``, so square brackets in tool output are
    never read as rich markup."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def show_event(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "thought":
            text = plain(_nothing_if_none(event.get("content")), keep_newlines=True).strip()
            if text:
                _print(self.console, Text(text, style="dim italic"))
        elif kind == "tool_call":
            name = _short(event.get("name", ""))
            args = event.get("args")
            try:
                summary = _short(subject_of(str(event.get("name", "")), args if isinstance(args, dict) else {}))
            except Exception:
                summary = ""
            line = Text(f"{bullet(self.console)} {name}", style="bold")
            if summary:
                line.append(f"  {summary}", style="dim")
            _print(self.console, line)
        elif kind == "tool_result":
            self._result(event.get("result"), bool(event.get("is_error")))
        elif kind == "final":
            content = plain(_nothing_if_none(event.get("content")), keep_newlines=True)
            _print(self.console, Markdown(content, hyperlinks=False))

    def _result(self, result: Any, is_error: bool) -> None:
        style = "red" if is_error else "dim"
        lines = _lines(plain(_nothing_if_none(result), keep_newlines=True))
        for line in lines[:MAX_RESULT_LINES]:
            _print(self.console, Text("  " + _short(line, 160), style=style))
        if len(lines) > MAX_RESULT_LINES:
            _print(self.console, Text(f"  ... {len(lines) - MAX_RESULT_LINES} more lines", style=style))

    def notice(self, text: str) -> None:
        _print(self.console, Text(plain(text, keep_newlines=True), style="cyan"))

    def error(self, text: str) -> None:
        _print(self.console, Text(plain(text, keep_newlines=True), style="bold red"))

    def team_plan(self, steps: Any) -> None:
        _print(self.console, Text("Team plan:", style="bold magenta"))
        for number, step in enumerate(steps if isinstance(steps, (list, tuple)) else [], 1):
            if not isinstance(step, dict):
                continue
            _print(self.console, Text(f"  {number}. {_short(step.get('agent'), 30)}: "
                                      f"{_short(step.get('query'), 200)}", style="dim"))

    def team_dispatch(self, agent: Any, query: Any) -> None:
        _print(self.console, Text(f"-> {_short(agent, 30)}: {_short(query, 200)}", style="bold magenta"))

    def team_result(self, agent: Any, ok: bool, text: Any) -> None:
        report = _short(_nothing_if_none(text), 160) or "(no report)"
        _print(self.console, Text(f"   {'done' if ok else 'failed'} ({_short(agent, 30)}): {report}",
                                  style="dim" if ok else "red"))

    def helper_call(self, helper: Any, name: Any, args: Any) -> None:
        """A helper's tool call, drawn like the main chat's with the helper's name in front."""
        try:
            summary = _short(subject_of(str(name), args if isinstance(args, dict) else {}))
        except Exception:
            summary = ""
        line = Text(f"{bullet(self.console)} [{_short(helper, 30)}] {_short(name, 40)}", style="bold")
        if summary:
            line.append(f"  {summary}", style="dim")
        _print(self.console, line)

    def team_question(self, question: Any) -> None:
        """The question, one label on the first line only and every other line indented, so that a question with
        line breaks cannot look like an approval panel (``Action:``) or its prompt (``Allow?``)."""
        text = plain(_nothing_if_none(question), keep_newlines=True)
        if len(text) > MAX_QUESTION_CHARS:
            text = text[:MAX_QUESTION_CHARS] + " ... (cut)"
        lines = _lines(text) or [""]
        shown = [("Team question: " if n == 0 else "  ") + line for n, line in enumerate(lines[:MAX_QUESTION_LINES])]
        if len(lines) > MAX_QUESTION_LINES:
            shown.append(f"  ... {len(lines) - MAX_QUESTION_LINES} more lines")
        _print(self.console, Text("\n".join(shown), style="bold yellow"))


def _nothing_if_none(value: Any) -> Any:
    return "" if value is None else value


def _preview_text(preview: Preview) -> Text:
    text = Text()
    for line in _lines(preview.body):
        style = ""
        if preview.kind == "diff":
            if line.startswith(("+++", "---")):
                style = "dim"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "cyan"
        text.append(plain(line) + "\n", style=style)
    return text


def _request_body(request: Request, preview: Preview, helper: str = "") -> Text:
    """The panel's content. The full action text (the preview title, which carries qualifiers such as "and
    everything inside it" or "saved under a new name") and the subject the gate judged are ordinary wrapped
    lines here, never a panel title, which would cut them."""
    body = Text()
    action = plain(preview.title)
    subject = plain(request.subject)
    body.append("Action: ", style="bold")
    body.append(action + "\n")
    helper = _short(" ".join(str(helper or "").split()), MAX_HELPER_CHARS)      # stripped, one line, cut
    if helper:
        body.append("Helper: ", style="bold")
        body.append(helper + "\n")
    # The subject is what the gate judged. Skip it only when it adds nothing: it is the action text itself,
    # or (for a command) the very command the body already shows, or a team job (its task is the body's
    # first line).
    shown_command = preview.kind == "command" and subject == " ".join(plain(preview.body).split())
    if subject != action and not shown_command and request.kind != "team":
        body.append("Subject: ", style="bold")
        body.append(subject + "\n")
    if preview.body:
        body.append("\n")
        body.append_text(_preview_text(preview))
    return body


CANCELLED = -1          # what a selection menu returns when the user pressed Esc, Ctrl+C or Ctrl+D


def select_menu(options: Sequence[str], default: int = 0,
                hotkeys: Optional[Mapping[str, int]] = None) -> Optional[int]:
    """A numbered list the user moves through with the arrow keys (or j / k), confirms with Enter, or picks by
    typing its number (or a hotkey such as ``y``). Returns the index chosen, ``CANCELLED`` for Esc / Ctrl+C /
    Ctrl+D, or None when no menu can be shown (not a terminal, or prompt_toolkit cannot start): the caller
    then asks in plain text. Option texts are printed through ``plain``."""
    if not options or not is_terminal():
        return None
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        labels = [plain(option) for option in options]
        position = [default if 0 <= default < len(labels) else 0]
        bindings = KeyBindings()

        def render():
            parts = []
            for number, label in enumerate(labels):
                if number == position[0]:
                    parts.append(("ansicyan bold", f"> {number + 1}. {label}\n"))
                else:
                    parts.append(("", f"  {number + 1}. {label}\n"))
            parts.append(("ansibrightblack", "Up / Down to move, Enter to confirm, Esc to cancel"))
            return FormattedText(parts)

        @bindings.add("up")
        @bindings.add("k")
        def _up(event):
            position[0] = (position[0] - 1) % len(labels)

        @bindings.add("down")
        @bindings.add("j")
        def _down(event):
            position[0] = (position[0] + 1) % len(labels)

        @bindings.add("enter")
        def _enter(event):
            event.app.exit(result=position[0])

        for number in range(min(len(labels), 9)):
            @bindings.add(str(number + 1))
            def _digit(event, number=number):
                event.app.exit(result=number)

        for key, index in (hotkeys or {}).items():
            if 0 <= index < len(labels) and len(key) == 1:
                @bindings.add(key)
                def _hot(event, index=index):
                    event.app.exit(result=index)

        @bindings.add("escape", eager=True)
        @bindings.add("c-c")
        @bindings.add("c-d")
        def _cancel(event):
            event.app.exit(result=CANCELLED)

        application = Application(
            layout=Layout(Window(FormattedTextControl(render), height=len(labels) + 1)),
            key_bindings=bindings, full_screen=False, erase_when_done=False)
        result = application.run()
        return result if isinstance(result, int) else CANCELLED
    except (KeyboardInterrupt, EOFError):
        return CANCELLED
    except Exception:
        return None


def make_asker(console: Console, read_line: Optional[Callable[[str], str]],
               cwd: Path,
               select: Optional[Callable[..., Optional[int]]] = None,
               helper: Optional[Callable[[], str]] = None) -> Callable[[Request], Answer]:
    """The gate's ``ask`` function: show what is about to happen, read y / a / n.

    ``cwd`` must be the SAME folder the gate was built with (the application passes the gate's ``cwd``), or the
    preview and the gate would look at different files. ``read_line`` defaults to plain ``input``.
    ``helper`` (optional) returns the name of the team helper whose step is running, shown as a ``Helper:``
    line.

    The prompt shows the full action text and ``Request.subject`` (what the gate judged, in its resolved
    form), never cut. A command, code or tool-argument text longer than ``MAX_COMMAND_CHARS`` cannot be shown
    whole, so it is declined unseen ("command too long to review") rather than approved on a partial view.

    Fails closed: bare Enter, end of input (Ctrl-D), Ctrl-C and any error reading an answer are all a "no".
    "Don't ask again" is offered only when the gate says it can remember it (``Request.remember``), and the
    prompt prints the exact rule (``Request.grant``) that answering ``a`` will remember."""
    read_line = read_line or input

    def ask(request: Request) -> Answer:
        preview = preview_for(request.tool, request.args, cwd)
        if preview.truncated:
            reason = preview.reason or "preview unavailable; nothing was run"
            _print(console, Text(f"Declined without asking ({plain(request.tool)}): {plain(reason)}.",
                                 style="bold red"))
            return Answer("n", reason)
        try:
            who = str(helper() or "") if helper is not None else ""
        except Exception:
            who = ""
        _print(console, Panel(_request_body(request, preview, who), title=Text(plain(request.tool)),
                              title_align="left"))
        always = bool(request.remember and request.grant)
        what = ""
        if always:
            if request.kind == "command":
                what = f"{plain(request.tool)} commands starting with '{plain(request.grant)}'"
            else:
                what = plain(request.grant)
        if select is not None:
            labels = ["Yes"]
            if always:
                labels.append(f"Yes, and don't ask again: {what}")
            labels.append("No, and tell the model what to do instead")
            hotkeys = {"y": 0, "n": len(labels) - 1}
            if always:
                hotkeys["a"] = 1
            _print(console, Text("Do you want to proceed?", style="bold"))
            try:
                picked = select(labels, 0, hotkeys)
            except Exception:
                picked = None
            if picked == CANCELLED:
                return Answer("n", "the user interrupted the question")
            if picked is not None:
                if picked == 0:
                    return Answer("y")
                if always and picked == 1:
                    return Answer("a")
                try:
                    reason = str(read_line("What should it do instead? (Enter to skip) ")).strip()
                except (Exception, KeyboardInterrupt):
                    reason = ""
                return Answer("n", reason)
        if always:
            _print(console, Text("[y] yes   [a] yes, don't ask again   [n] no", style="bold"))
            _print(console, Text("'a' will remember: " + what, style="yellow"))
        else:
            _print(console, Text("[y] yes   [n] no", style="bold"))
        choices = {"y": "y", "yes": "y", "n": "n", "no": "n", "": "n"}
        if always:
            choices.update({"a": "a", "always": "a"})
        while True:
            try:
                raw = read_line("Allow? [y/a/n] " if always else "Allow? [y/n] ")
            except EOFError:
                return Answer("n", "no answer was given")
            except KeyboardInterrupt:
                return Answer("n", "the user interrupted the question")
            except Exception:
                return Answer("n", "could not read an answer")
            choice = choices.get(str(raw).strip().lower())
            if choice is None:
                _print(console, Text("Please answer y, a or n." if always else "Please answer y or n.",
                                     style="yellow"))
                continue
            if choice == "n":
                try:
                    reason = str(read_line("Reason (optional): ")).strip()
                except (Exception, KeyboardInterrupt):     # already declined; the reason is optional
                    reason = ""
                return Answer("n", reason)
            return Answer(choice)

    return ask


def make_completer(commands: Sequence[str], descriptions: Optional[Mapping[str, str]] = None):
    """Completion: ``/commands`` at the start of a line (offered as you type, each with its one-line
    description), file paths for any other word (only when Tab is pressed, so typing a message stays quiet)."""
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    notes = dict(descriptions or {})

    class _Completer(Completer):
        def __init__(self) -> None:
            self._paths = PathCompleter(expanduser=True)

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text:
                for name in commands:
                    if name.startswith(text):
                        yield Completion(name, start_position=-len(text),
                                         display_meta=plain(notes.get(name, "")) or None)
                return
            if not complete_event.completion_requested:
                return
            word = document.get_word_before_cursor(WORD=True)
            if word:
                yield from self._paths.get_completions(Document(word), complete_event)

    return _Completer()


def is_terminal() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


def safe_toolbar(toolbar: Callable[[], Any]) -> Callable[[], Any]:
    """The status bar text with control characters defused; an error building it gives an empty bar."""
    def render() -> Any:
        try:
            text = toolbar()
        except Exception:
            return ""
        return plain(text) if isinstance(text, str) else text
    return render


class PromptInput:
    """The chat prompt: history kept between runs, Alt+Enter for a new line, Tab completion, a status bar,
    Shift+Tab to cycle the mode. ``read_short`` is a plain one-line prompt (approvals, choices) that does not
    touch the history.

    Without a console (stdin or stdout is not a terminal, or prompt_toolkit cannot start, as on Windows
    without a console buffer) both fall back to plain ``input()``.

    The history file stores whatever the user types, and is made readable by the user only where the platform
    allows it."""

    def __init__(self, *, history_file: Path, commands: Sequence[str], toolbar: Callable[[], str],
                 cycle_mode: Callable[[], object], descriptions: Optional[Mapping[str, str]] = None) -> None:
        self._chat = None
        self._short = None
        history_file = Path(history_file)
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history_file.touch(exist_ok=True)
            history_file.chmod(0o600)
        except OSError:
            pass
        if not is_terminal():
            return
        try:
            self._chat, self._short = self._sessions(history_file, commands, safe_toolbar(toolbar), cycle_mode,
                                                     descriptions)
        except Exception:
            self._chat = self._short = None

    @staticmethod
    def _sessions(history_file: Path, commands: Sequence[str], toolbar: Callable[[], Any],
                  cycle_mode: Callable[[], object], descriptions: Optional[Mapping[str, str]] = None):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        bindings = KeyBindings()

        @bindings.add("enter")
        def _send(event):
            buffer = event.current_buffer
            state = buffer.complete_state
            if state is not None and state.current_completion is not None:     # a menu item is highlighted:
                buffer.apply_completion(state.current_completion)             # Enter picks it, a second sends
                return
            buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        def _cycle(event):
            cycle_mode()
            event.app.invalidate()

        # A blinking cursor says "waiting for you"; a still one looks like a frozen program. Older
        # prompt_toolkit versions have no such option, and then the cursor stays as the terminal draws it.
        blink: Dict[str, Any] = {}
        try:
            from prompt_toolkit.cursor_shapes import CursorShape
            blink["cursor"] = CursorShape.BLINKING_BEAM
        except Exception:
            pass

        def build(**options):
            try:
                return PromptSession(**options, **blink)
            except TypeError:
                return PromptSession(**options)

        chat = build(history=FileHistory(str(history_file)), multiline=True,
                     key_bindings=bindings, completer=make_completer(commands, descriptions),
                     bottom_toolbar=toolbar, complete_while_typing=True)
        short = build(multiline=False, bottom_toolbar=toolbar)
        return chat, short

    def read(self, prompt: str = "> ") -> str:
        if self._chat is None:
            return input(prompt)
        return self._chat.prompt([("ansicyan bold", prompt)])        # a coloured prompt: your turn

    def read_short(self, prompt: str = "") -> str:
        if self._short is None:
            return input(prompt)
        return self._short.prompt([("ansiyellow bold", prompt)])

    def select(self, options: Sequence[str], default: int = 0, hotkeys: Optional[Mapping[str, int]] = None):
        """An arrow-key menu (see ``select_menu``); None when this input line has no console to draw on."""
        if self._chat is None:
            return None
        return select_menu(options, default, hotkeys)
