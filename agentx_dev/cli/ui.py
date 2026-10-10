"""Terminal rendering (rich) and input (prompt_toolkit) for the agentx command.

``prompt_toolkit`` is imported inside ``make_completer`` and ``PromptInput`` only, so everything else here
works without it and can be tested without a terminal.

Everything the model or the project supplies (tool arguments, previews, file content, thoughts, titles, the
final answer) is printed as rich ``Text`` after ``plain`` has defused control characters and escape
sequences, so it can neither style nor spoof the interface. Only the final answer is rendered as Markdown
(with hyperlinks off)."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, Sequence

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from agentx_dev.cli.approval import Answer, Request, subject_of
from agentx_dev.cli.preview import Preview, preview_for

MAX_RESULT_LINES = 3
MAX_SUMMARY_CHARS = 100

# Format and separator characters that can reorder, hide or break up text (bidi overrides and isolates,
# zero-width spaces, word joiners, line and paragraph separators, BOM, soft hyphen). Joiners that emoji
# sequences need (U+200C, U+200D) are left alone.
_UNSAFE = frozenset(
    [0x00AD, 0x061C, 0x200B, 0x200E, 0x200F, 0x2028, 0x2029, 0xFEFF]
    + list(range(0x202A, 0x202F)) + list(range(0x2060, 0x2065)) + list(range(0x2066, 0x206A))
)


def _escape(char: str) -> str:
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def plain(text: Any, *, keep_newlines: bool = False) -> str:
    """``text`` made safe to print: control characters (ESC, BEL, CR, C1 codes...), lone surrogates and
    direction overrides are replaced by a visible escape such as ``\\x1b``. Tabs stay. Line breaks stay when
    ``keep_newlines`` is true (the text is first split into lines, so CR LF becomes one break) and become a
    visible ``\\n`` otherwise."""
    try:
        text = text if isinstance(text, str) else str(text)
    except Exception:
        return "(cannot be shown)"
    if keep_newlines:
        text = "\n".join(text.splitlines())
    out = []
    for char in text:
        if char == "\t" or (char == "\n" and keep_newlines):
            out.append(char)
        elif char == "\n":
            out.append("\\n")
        elif ord(char) in _UNSAFE or unicodedata.category(char) in ("Cc", "Cs"):
            out.append(_escape(char))
        else:
            out.append(char)
    return "".join(out)


def bullet(console: Console) -> str:
    """The marker in front of a tool call; a plain star on a console that cannot print the dot."""
    return "●" if "utf" in (console.encoding or "").lower() else "*"


def _short(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    flat = " ".join(plain(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class Screen:
    """Draws what the agent does. Everything is printed as ``Text``, so square brackets in tool output are
    never read as rich markup."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def show_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "thought":
            text = plain(event.get("content", ""), keep_newlines=True).strip()
            if text:
                self.console.print(Text(text, style="dim italic"))
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
            self.console.print(line)
        elif kind == "tool_result":
            self._result(event.get("result", ""), bool(event.get("is_error")))
        elif kind == "final":
            self.console.print(Markdown(plain(event.get("content", ""), keep_newlines=True), hyperlinks=False))

    def _result(self, result: Any, is_error: bool) -> None:
        style = "red" if is_error else "dim"
        lines = str(result).splitlines() or [""]
        for line in lines[:MAX_RESULT_LINES]:
            self.console.print(Text("  " + _short(line, 160), style=style))
        if len(lines) > MAX_RESULT_LINES:
            self.console.print(Text(f"  ... {len(lines) - MAX_RESULT_LINES} more lines", style=style))

    def notice(self, text: str) -> None:
        self.console.print(Text(plain(text, keep_newlines=True), style="cyan"))

    def error(self, text: str) -> None:
        self.console.print(Text(plain(text, keep_newlines=True), style="bold red"))


def _preview_text(preview: Preview) -> Text:
    text = Text()
    for line in preview.body.splitlines() or [""]:
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


def make_asker(console: Console, read_line: Callable[[str], str], cwd: Path) -> Callable[[Request], Answer]:
    """The gate's ``ask`` function: show what is about to happen, read y / a / n.

    Fails closed: bare Enter, end of input (Ctrl-D) and Ctrl-C are all a "no". "Don't ask again" is offered
    only when the gate says it can remember it (``Request.remember``), and the prompt prints the exact rule
    (``Request.grant``) that answering ``a`` will remember."""

    def ask(request: Request) -> Answer:
        preview = preview_for(request.tool, request.args, cwd)
        console.print(Panel(_preview_text(preview), title=Text(plain(preview.title)), title_align="left"))
        always = bool(request.remember and request.grant)
        if always:
            console.print(Text("[y] yes   [a] yes, don't ask again   [n] no", style="bold"))
            console.print(Text("'a' will remember: " + plain(request.grant), style="yellow"))
        else:
            console.print(Text("[y] yes   [n] no", style="bold"))
        choices = {"y": "y", "yes": "y", "n": "n", "no": "n", "": "n"}
        if always:
            choices.update({"a": "a", "always": "a"})
        while True:
            try:
                raw = read_line("Allow? [y/n] " if not always else "Allow? [y/a/n] ")
            except EOFError:
                return Answer("n", "no answer was given")
            except KeyboardInterrupt:
                return Answer("n", "the user interrupted the question")
            choice = choices.get(raw.strip().lower())
            if choice is None:
                console.print(Text("Please answer y, a or n." if always else "Please answer y or n.",
                                   style="yellow"))
                continue
            if choice == "n":
                try:
                    reason = read_line("Reason (optional): ").strip()
                except (EOFError, KeyboardInterrupt):
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


class PromptInput:
    """The chat prompt: history kept between runs, Alt+Enter for a new line, Tab completion, a status bar,
    Shift+Tab to cycle the mode. ``read_short`` is a plain one-line prompt (approvals, choices) that does not
    touch the history."""

    def __init__(self, *, history_file: Path, commands: Sequence[str], toolbar: Callable[[], str],
                 cycle_mode: Callable[[], object]) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        history_file = Path(history_file)
        history_file.parent.mkdir(parents=True, exist_ok=True)
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

        self._chat = PromptSession(history=FileHistory(str(history_file)), multiline=True,
                                   key_bindings=bindings, completer=make_completer(commands),
                                   bottom_toolbar=toolbar, complete_while_typing=False)
        self._short = PromptSession(multiline=False, bottom_toolbar=toolbar)

    def read(self, prompt: str = "> ") -> str:
        return self._chat.prompt(prompt)

    def read_short(self, prompt: str = "") -> str:
        return self._short.prompt(prompt)
