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
from typing import Any, Callable, Dict, List, Optional, Sequence

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from agentx_dev.cli.approval import Answer, Request, subject_of
from agentx_dev.cli.preview import Preview, preview_for

MAX_RESULT_LINES = 3
MAX_SUMMARY_CHARS = 100

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
    with console.capture() as capture:
        console.print(*renderables)
    data = capture.get()
    encoding = console.encoding or "ascii"
    try:
        safe = data.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        safe = data.encode("ascii", "backslashreplace").decode("ascii")
    console.file.write(safe)
    console.file.flush()


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


def _request_body(request: Request, preview: Preview) -> Text:
    """The panel's content. The full action text (the preview title, which carries qualifiers such as "and
    everything inside it" or "saved under a new name") and the subject the gate judged are ordinary wrapped
    lines here, never a panel title, which would cut them."""
    body = Text()
    action = plain(preview.title)
    subject = plain(request.subject)
    body.append("Action: ", style="bold")
    body.append(action + "\n")
    # The subject is what the gate judged. Skip it only when it adds nothing: it is the action text itself,
    # or (for a command) the very command the body already shows.
    shown_command = preview.kind == "command" and subject == " ".join(plain(preview.body).split())
    if subject != action and not shown_command:
        body.append("Subject: ", style="bold")
        body.append(subject + "\n")
    if preview.body:
        body.append("\n")
        body.append_text(_preview_text(preview))
    return body


def make_asker(console: Console, read_line: Optional[Callable[[str], str]],
               cwd: Path) -> Callable[[Request], Answer]:
    """The gate's ``ask`` function: show what is about to happen, read y / a / n.

    ``cwd`` must be the SAME folder the gate was built with (the application passes the gate's ``cwd``), or the
    preview and the gate would look at different files. ``read_line`` defaults to plain ``input``.

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
        _print(console, Panel(_request_body(request, preview), title=Text(plain(request.tool)),
                              title_align="left"))
        always = bool(request.remember and request.grant)
        if always:
            _print(console, Text("[y] yes   [a] yes, don't ask again   [n] no", style="bold"))
            if request.kind == "command":
                what = f"{plain(request.tool)} commands starting with '{plain(request.grant)}'"
            else:
                what = plain(request.grant)
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


def make_completer(commands: Sequence[str]):
    """Tab completion: ``/commands`` at the start of a line, file paths for any other word."""
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    class _Completer(Completer):
        def __init__(self) -> None:
            self._paths = PathCompleter(expanduser=True)

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text:
                for name in commands:
                    if name.startswith(text):
                        yield Completion(name, start_position=-len(text))
                return
            word = document.get_word_before_cursor(WORD=True)
            if word:
                yield from self._paths.get_completions(Document(word), complete_event)

    return _Completer()


def _has_console() -> bool:
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
                 cycle_mode: Callable[[], object]) -> None:
        self._chat = None
        self._short = None
        history_file = Path(history_file)
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history_file.touch(exist_ok=True)
            history_file.chmod(0o600)
        except OSError:
            pass
        if not _has_console():
            return
        try:
            self._chat, self._short = self._sessions(history_file, commands, safe_toolbar(toolbar), cycle_mode)
        except Exception:
            self._chat = self._short = None

    @staticmethod
    def _sessions(history_file: Path, commands: Sequence[str], toolbar: Callable[[], Any],
                  cycle_mode: Callable[[], object]):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        bindings = KeyBindings()

        @bindings.add("enter")
        def _send(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        def _cycle(event):
            cycle_mode()
            event.app.invalidate()

        chat = PromptSession(history=FileHistory(str(history_file)), multiline=True,
                             key_bindings=bindings, completer=make_completer(commands),
                             bottom_toolbar=toolbar, complete_while_typing=False)
        short = PromptSession(multiline=False, bottom_toolbar=toolbar)
        return chat, short

    def read(self, prompt: str = "> ") -> str:
        if self._chat is None:
            return input(prompt)
        return self._chat.prompt(prompt)

    def read_short(self, prompt: str = "") -> str:
        if self._short is None:
            return input(prompt)
        return self._short.prompt(prompt)
