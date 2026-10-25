"""Terminal rendering (rich) and input (prompt_toolkit) for the agentx command.

``prompt_toolkit`` is imported inside ``make_completer`` and ``PromptInput`` only, so everything else here
works without it and can be tested without a terminal.

Everything the model or the project supplies (tool arguments, previews, file content, thoughts, titles, the
final answer) is printed as rich ``Text`` after ``plain`` has defused control characters and escape
sequences, so it can neither style nor spoof the interface. Only the final answer is rendered as Markdown
(with hyperlinks off). Nothing here may kill a turn because of the console: text the console cannot encode is
printed with backslash escapes, and any failure to read an answer is a "no"."""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from agentx_dev.cli.approval import Answer, Request, subject_of
from agentx_dev.cli.preview import Preview, preview_for

MAX_RESULT_LINES = 3
MAX_SUMMARY_CHARS = 100
MAX_QUESTION_LINES = 12
MAX_QUESTION_CHARS = 1500
MAX_HELPER_CHARS = 110             # helper and agent names: explorer@claude-sonnet-4-5-20250929 shows whole

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


# -- the look: the agentx mark (a prompt chevron and a cursor bar) in the brand's lime ---------------------------

ACCENT = "#b8ff3e"
LOGO = ("█▄      ██",
        " ▀█▄    ██",
        " ▄█▀    ██",
        "█▀      ██")
MODE_LINES = {                      # mode -> (marker, words, style) for the line under the input
    "default": ("", "default mode: edits and commands ask first", "ansiwhite"),
    "accept-edits": ("⏵⏵ ", "accept edits on", "ansimagenta bold"),
    "plan": ("⏸ ", "plan mode on", "ansicyan bold"),
    "auto": ("⏵⏵ ", "auto mode on", f"{ACCENT} bold"),
}


def welcome_panel(*, version: str, greeting: str, model: str, folder: str, tips: Sequence[str],
                  news: Sequence[str], width: int, extra: Sequence[str] = ()) -> Panel:
    """The box drawn when agentx starts: the greeting, the mark, the model and the folder on the left; tips and
    what is new on the right (below the left part on a narrow screen). Every text is plain: nothing in it is
    read as markup."""
    left = Table.grid()
    left.add_column(justify="center", no_wrap=True, overflow="ellipsis")
    left.add_row(Text(plain(greeting), style="bold"))
    left.add_row("")
    for row in LOGO:
        left.add_row(Text(row, style=ACCENT))
    left.add_row("")
    left.add_row(Text(plain(model), style="dim"))
    left.add_row(Text(plain(folder), style="dim"))
    for line in extra:
        left.add_row(Text(plain(line), style="dim"))
    right = Table.grid()
    right.add_column(no_wrap=True, overflow="ellipsis")
    right.add_row(Text("Tips for getting started", style=f"bold {ACCENT}"))
    for tip in tips:
        right.add_row(Text(plain(tip)))
    right.add_row(Rule(style="dim"))
    right.add_row(Text("What's new", style=f"bold {ACCENT}"))
    for item in news:
        right.add_row(Text(plain(item)))
    right.add_row(Text("/help lists the commands", style="dim italic"))
    if width < 90:
        body: Any = Group(left, Rule(style="dim"), right)
    else:
        height = max(len(left.rows), len(right.rows))
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=max(30, min(44, width // 3)), justify="center")
        grid.add_column(width=1, style="dim")
        grid.add_column(ratio=1)
        grid.add_row(left, Text("\n".join("│" * height)), right)
        body = grid
    return Panel(body, title=Text(f" agentx v{plain(version)} ", style=ACCENT), title_align="left",
                 border_style=ACCENT, box=box.ROUNDED, padding=(0, 1))


class Callout:
    """A note with a bar down its left side: the title in bold, the rest dim, each wrapped line barred."""

    def __init__(self, title: str, body: str = "", bar_style: str = ACCENT) -> None:
        text = Text(plain(title), style="bold")
        if body:
            text.append("\n" + plain(body, keep_newlines=True), style="dim")
        self.text = text
        self.bar_style = bar_style

    def __rich_console__(self, console: Console, options: Any):
        lines = console.render_lines(self.text, options.update(width=max(1, options.max_width - 2)), pad=False)
        bar = Segment("▎ ", Style.parse(self.bar_style))
        for line in lines:
            yield bar
            yield from line
            yield Segment.line()


def status_line(mode: str, model: str, folder: str, width: Optional[int] = None) -> List[Tuple[str, str]]:
    """The two lines under the input, as prompt_toolkit text: a rule, then the mode (and how to change it), the
    model and the folder's name."""
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    marker, words, style = MODE_LINES.get(mode, MODE_LINES["default"])
    return [("class:rule", "─" * max(1, width - 1) + "\n"),
            (style, f" {marker}{words}"), ("class:status", " (shift+tab to cycle)"),
            ("class:status", f" · {plain(model)} · {plain(folder)}")]


def prompt_message(width: Optional[int] = None) -> List[Tuple[str, str]]:
    """What stands before the input: a rule across the screen, then the prompt mark."""
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    return [("class:rule", "─" * max(1, width - 1) + "\n"), ("class:prompt", "❯ ")]


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

    def callout(self, title: str, body: str = "") -> None:
        _print(self.console, Callout(title, body))

    def warning(self, text: str, hint: str = "") -> None:
        line = Text("⚠ ", style="bold yellow")
        line.append(plain(text), style="bold yellow")
        if hint:
            line.append(" · " + plain(hint), style="dim")
        _print(self.console, line)

    def welcome(self, **parts: Any) -> None:
        _print(self.console, welcome_panel(width=self.console.width, **parts))

    def error(self, text: str) -> None:
        _print(self.console, Text(plain(text, keep_newlines=True), style="bold red"))

    def team_plan(self, steps: Any) -> None:
        _print(self.console, Text("Team plan:", style="bold magenta"))
        for number, step in enumerate(steps if isinstance(steps, (list, tuple)) else [], 1):
            if not isinstance(step, dict):
                continue
            _print(self.console, Text(f"  {number}. {_short(step.get('agent'), MAX_HELPER_CHARS)}: "
                                      f"{_short(step.get('query'), 200)}", style="dim"))

    def team_dispatch(self, agent: Any, query: Any) -> None:
        _print(self.console, Text(f"-> {_short(agent, MAX_HELPER_CHARS)}: {_short(query, 200)}",
                                  style="bold magenta"))

    def team_result(self, agent: Any, ok: bool, text: Any, partial: bool = False) -> None:
        report = _short(_nothing_if_none(text), 160) or "(no report)"
        mark = ("partial" if partial else "done") if ok else "failed"
        _print(self.console, Text(f"   {mark} ({_short(agent, MAX_HELPER_CHARS)}): {report}",
                                  style="dim" if ok else "red"))

    def helper_call(self, helper: Any, name: Any, args: Any) -> None:
        """A helper's tool call, drawn like the main chat's with the helper's name in front."""
        try:
            summary = _short(subject_of(str(name), args if isinstance(args, dict) else {}))
        except Exception:
            summary = ""
        line = Text(f"{bullet(self.console)} [{_short(helper, MAX_HELPER_CHARS)}] {_short(name, 40)}",
                    style="bold")
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
               helper: Optional[Callable[[], str]] = None,
               helper_models: Optional[Callable[[], str]] = None) -> Callable[[Request], Answer]:
    """The gate's ``ask`` function: show what is about to happen, read y / a / n.

    ``cwd`` must be the SAME folder the gate was built with (the application passes the gate's ``cwd``), or the
    preview and the gate would look at different files. ``read_line`` defaults to plain ``input``.
    ``helper`` (optional) returns the name of the team helper whose step is running, shown as a ``Helper:``
    line. ``helper_models`` (optional) returns the ``Helper models:`` line for a ``start_team_job`` prompt.

    The prompt shows the full action text and ``Request.subject`` (what the gate judged, in its resolved
    form), never cut. A command, code or tool-argument text longer than ``MAX_COMMAND_CHARS`` cannot be shown
    whole, so it is declined unseen ("command too long to review") rather than approved on a partial view.

    Fails closed: bare Enter, end of input (Ctrl-D), Ctrl-C and any error reading an answer are all a "no".
    "Don't ask again" is offered only when the gate says it can remember it (``Request.remember``), and the
    prompt prints the exact rule (``Request.grant``) that answering ``a`` will remember."""
    read_line = read_line or input

    def ask(request: Request) -> Answer:
        line = ""
        if helper_models is not None and request.tool == "start_team_job":
            try:
                value = helper_models()
                line = plain(value) if isinstance(value, str) else ""
            except Exception:                         # a broken source must never stop the question
                line = ""
        preview = preview_for(request.tool, request.args, cwd, helper_models=line)
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


_SLASH_START = re.compile(r"(\s*)/([A-Za-z0-9_-]*)")
_STYLE_CLASS = {"command": "class:command", "skill": "class:skill", "unknown": "class:unknown-command",
                "partial": "class:partial-command"}


def classify_command(line: str, commands: Sequence[str],
                     skills: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, int]]:
    """``(kind, end)`` for a line that starts with a slash command, where ``end`` is where the ``/name`` ends;
    None for anything else (a message, ``/tmp/build.log``, ``/README.md``, ``/ is the root``). ``kind`` is
    ``"command"``, ``"skill"``, ``"partial"`` (the start of a command or skill, still being typed) or ``"unknown"``.
    The same reading as ``parse_slash``: a plain name right after the slash, then a space or the end."""
    match = _SLASH_START.match(line or "")
    if match is None:
        return None
    end = match.end()
    rest = line[end:]
    if rest and not rest[0].isspace():
        return None                                   # "/tmp/build.log", "/README.md": a message
    name = match.group(2)
    if not name:
        return ("partial", end) if not rest else None
    slash = "/" + name.lower()
    known = {command.lower() for command in commands}
    offered = {skill.lower() for skill in (skills or {})}
    if slash in known:
        return "command", end
    if slash in offered:
        return "skill", end
    if not rest and any(item.startswith(slash) for item in known | offered):
        return "partial", end
    return "unknown", end


def make_lexer(commands: Sequence[str], extra: Optional[Callable[[], Mapping[str, str]]] = None):
    """Colours for the input line: the ``/name`` at the start gets the class of ``classify_command`` (a known
    command, a skill, an unknown name, a name still being typed); everything else is plain. ``extra`` returns the
    skills (``{"/name": description}``) and is asked each time, like the completer's."""
    from prompt_toolkit.lexers import Lexer

    class _Lexer(Lexer):
        def lex_document(self, document):
            lines = document.lines
            try:
                skills = dict(extra()) if extra is not None else {}
            except Exception:
                skills = {}
            first = classify_command(lines[0] if lines else "", commands, skills)

            def get_line(number: int):
                text = lines[number] if number < len(lines) else ""
                if number != 0 or first is None:
                    return [("", text)]
                kind, end = first
                start = len(text) - len(text.lstrip())
                parts = [("", text[:start])] if start else []
                parts.append((_STYLE_CLASS[kind], text[start:end]))
                if end < len(text):
                    parts.append(("", text[end:]))
                return parts

            return get_line

    return _Lexer()


def input_style():
    """The input line's colours: a command in cyan, a skill in magenta, an unknown name in red, a name still being
    typed in plain cyan; and a pop-up whose selected row stands out."""
    from prompt_toolkit.styles import Style
    return Style.from_dict({
        "command": "ansicyan bold",
        "skill": "ansimagenta bold",
        "unknown-command": "ansired",
        "partial-command": "ansicyan",
        "completion-menu": "bg:#262626 #d0d0d0",
        "completion-menu.completion": "bg:#262626 #d0d0d0",
        "completion-menu.completion.current": "bg:ansicyan #000000 bold",
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "noreverse",
        "rule": "#5a5a62",
        "status": "#8b8b93",
        "prompt": f"{ACCENT} bold",
        "completion-menu.meta.completion": "bg:#303030 #9a9a9a",
        "completion-menu.meta.completion.current": "bg:ansicyan #000000",
    })


def make_completer(commands: Sequence[str], descriptions: Optional[Mapping[str, str]] = None,
                   extra: Optional[Callable[[], Mapping[str, str]]] = None):
    """Completion: ``/commands`` at the start of a line (offered as you type, each with its one-line
    description), file paths for any other word (only when Tab is pressed, so typing a message stays quiet).

    ``extra`` returns more ``{"/name": description}`` entries (the skills); it is asked each time the list is
    drawn, so one added meanwhile shows at once. A command is never replaced by an extra of the same name, and
    an extra that fails is left out. An extra is completed with a space after it: you type what you want next."""
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
                try:
                    more = dict(extra()) if extra is not None else {}
                except Exception:
                    more = {}
                for name in more:
                    if name.startswith(text) and name not in commands:
                        yield Completion(name + " ", start_position=-len(text),
                                         display_meta=plain(more.get(name, "")) or None)
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
                 cycle_mode: Callable[[], object], descriptions: Optional[Mapping[str, str]] = None,
                 extra_commands: Optional[Callable[[], Mapping[str, str]]] = None) -> None:
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
                                                     descriptions, extra_commands)
        except Exception:
            self._chat = self._short = None

    @staticmethod
    def _sessions(history_file: Path, commands: Sequence[str], toolbar: Callable[[], Any],
                  cycle_mode: Callable[[], object], descriptions: Optional[Mapping[str, str]] = None,
                  extra: Optional[Callable[[], Mapping[str, str]]] = None):
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
                     key_bindings=bindings, completer=make_completer(commands, descriptions, extra),
                     bottom_toolbar=toolbar, complete_while_typing=True,
                     lexer=make_lexer(commands, extra), style=input_style())
        short = build(multiline=False, bottom_toolbar=toolbar, style=input_style())
        return chat, short

    def read(self, prompt: str = "> ") -> str:
        if self._chat is None:
            return input(prompt)
        return self._chat.prompt(prompt_message)        # a rule across the screen, then the mark: your turn

    def read_short(self, prompt: str = "") -> str:
        if self._short is None:
            return input(prompt)
        return self._short.prompt([("ansiyellow bold", prompt)])

    def select(self, options: Sequence[str], default: int = 0, hotkeys: Optional[Mapping[str, int]] = None):
        """An arrow-key menu (see ``select_menu``); None when this input line has no console to draw on."""
        if self._chat is None:
            return None
        return select_menu(options, default, hotkeys)
