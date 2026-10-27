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
import time
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
from agentx_dev.cli.ui_text import (  # noqa: F401  (re-exported: older code imports these here)
    _CAUTIONS, _command_output, _COUNT, _escape, _ESCAPED, _EXIT, _first_and_more, _JOINERS, _lines, _MARKDOWN,
    _normalised, _nothing_if_none, _NUMBERED, _OF_TOTAL, _plural, _SCANNED, _short, _times, _UNSAFE, ACCENT, bullet,
    elbow, helper_activity, HELPER_LOOKS, MAX_SUMMARY_CHARS, plain, report_line, short_time, short_tokens,
    SKILL_TOOL, step_counts, STEP_KINDS, step_stats, summarize_result, token_text,
)
from agentx_dev.cli.ui_input import (  # noqa: F401  (re-exported: older code imports these here)
    _SLASH_START, _STYLE_CLASS, classify_command, input_style, make_completer, make_lexer, safe_toolbar,
)

MAX_RESULT_LINES = 3
MAX_QUESTION_LINES = 12
MAX_QUESTION_CHARS = 1500
MAX_HELPER_CHARS = 110             # helper and agent names: explorer@claude-sonnet-4-5-20250929 shows whole


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
        # ``board()``, when set, returns lines drawn above the spinner (the team's running helpers), or None
        self.board: Optional[Callable[[], Any]] = None
        # Full-screen mode draws the spinner itself: then start/stop only set what ``renderable`` returns, and call
        # ``drawn_by()`` so the screen redraws.
        self.drawn_by: Optional[Callable[[], None]] = None
        self._shown = False

    @property
    def enabled(self) -> bool:
        try:
            return bool(self.console.is_terminal)
        except Exception:
            return False

    def __rich__(self):
        if self._spinner is not None:
            self._spinner.update(text=Text(f"{self._label}... {int(time.monotonic() - self._since)}s", style="dim"))
        extra = None
        if self.board is not None:
            try:
                extra = self.board()
            except Exception:                        # the block is a help: a drawing error must not stop the spinner
                extra = None
        return Group(extra, self._spinner) if extra is not None and self._spinner is not None else self._spinner

    def renderable(self) -> Any:
        """What full-screen mode draws for the spinner (with its block), or None when it is not running."""
        return self.__rich__() if self._shown and self._spinner is not None else None

    def start(self, label: str = "Thinking") -> None:
        self.stop()
        if not self.enabled and self.drawn_by is None:
            return
        from rich.live import Live
        from rich.spinner import Spinner
        self._label = plain(label)
        self._since = time.monotonic()
        utf = (getattr(self.console, "encoding", "") or "").lower().startswith("utf")
        self._spinner = Spinner("dots" if utf else "line", text=Text(f"{self._label}...", style="dim"))
        if self.drawn_by is not None:
            self._shown = True
            self._redraw()
            return
        try:
            self._live = Live(self, console=self.console, transient=True, refresh_per_second=10)
            self._live.start()
        except Exception:
            self._live = None

    def _redraw(self) -> None:
        try:
            self.drawn_by()                          # type: ignore[misc]
        except Exception:
            pass

    def stop(self) -> None:
        if self._shown:
            self._shown = False
            self._redraw()
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
            running = self._live is not None or self._shown
            label = self._label
            self.stop()
            try:
                yield
            finally:
                if running:
                    self.start(label)
        return block()


# -- the look: the agentx mark (a prompt chevron and a cursor bar) in the brand's lime ---------------------------

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
    never read as rich markup.

    The agent looking around (skill reads, recall, reads, searches, listings, web calls) is one flow, drawn as a
    tree and redrawn in place above the spinner while it grows (``flow_block``), printed when the agent moves on
    (``flush``); a skill's text is never shown, and skill reads in a row are one step (``Skills  a → b``). The
    agent's notes that lead into such a step are narration and are not printed.

    A team helper's looking calls (reads, searches, listings, web, skills) are counted the same way, one line per
    helper (``● [explorer] Read 12 files · Searched 3 patterns``), printed when that helper does something else or
    its step ends; ``helpers_line()`` is the spinner's text meanwhile. Paths under ``cwd`` are shown relative."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.cwd: Optional[str] = None
        # full-screen mode: header(factory, compact) shows the welcome box as the fixed header
        self.header: Optional[Callable[[Callable[[int], Any], str], None]] = None
        self._flow: List[Dict[str, Any]] = []        # the open look-around flow: one item per step
        self._held: List[str] = []                   # the agent's notes, until the next event says what they lead to
        self._helpers: Dict[str, Dict[str, Any]] = {}   # helper -> what its looking calls have done so far

    def _sep(self) -> str:
        return " · " if "utf" in (self.console.encoding or "").lower() else ", "

    def _relative(self, text: str) -> str:
        """``text`` with the project folder taken off the front of the paths in it."""
        if not self.cwd or not text:
            return text
        root = self.cwd.rstrip("\\/")
        flags = re.IGNORECASE if sys.platform == "win32" else 0
        for prefix in {root + "\\", root + "/", root.replace("\\", "/") + "/"}:
            text = re.sub(re.escape(prefix), "", text, flags=flags)
        return text

    def helpers_line(self) -> str:
        sep = self._sep()
        return "; ".join(f"{helper}: {helper_activity(counts, sep)}" for helper, counts in self._helpers.items()
                         if helper_activity(counts, sep))

    def flush_helper(self, helper: Any) -> None:
        """Print what ``helper``'s looking calls did as one line, and start counting again."""
        counts = self._helpers.pop(_short(helper, MAX_HELPER_CHARS), None)
        activity = helper_activity(counts or {}, self._sep())
        if not activity:
            return
        line = Text(f"{bullet(self.console)} [{_short(helper, MAX_HELPER_CHARS)}] ", style="bold")
        line.append(activity, style="dim")
        _print(self.console, line)

    def flush_helpers(self) -> None:
        for helper in list(self._helpers):
            self.flush_helper(helper)

    def _utf(self) -> bool:
        return "utf" in (self.console.encoding or "").lower()

    def team_board(self, rows: Sequence[Any], done: int = 0, total: int = 0) -> Optional[Group]:
        """The block above the spinner while a team job runs: one line per running step (helper · label, what it
        has done so far, time and tokens), updated in place. None when no step runs."""
        rows = list(rows)
        if not rows:
            return None
        sep = self._sep() if self._utf() else ", "
        head = Text(f"{bullet(self.console)} Team", style=f"bold {ACCENT}")
        head.append(f" · {len(rows)} running" + (f" · {done} of {total} done" if total else ""), style="dim")
        lines: List[Any] = [head]
        for number, row in enumerate(rows):
            try:
                snapshot = row.snapshot()
            except Exception:
                continue
            last = number == len(rows) - 1
            branch = ("└ " if last else "├ ") if self._utf() else ("`- " if last else "|- ")
            line = Text(f"  {branch}", style="dim", no_wrap=True, overflow="ellipsis")
            line.append(f"{_short(row.helper, MAX_HELPER_CHARS)} · {_short(row.label, 40)}", style="bold")
            activity = helper_activity(step_counts(snapshot), sep)
            line.append(f"  {activity or 'starting'}", style="dim")
            line.append(f"  {short_time(snapshot['seconds'])} · {token_text(snapshot)}", style="dim")
            lines.append(line)
        return Group(*lines)

    def team_done(self, agent: Any, label: Any, ok: bool, text: Any, *, partial: bool = False,
                  skipped: bool = False, status: Any = None) -> None:
        """A step's permanent line when it ends: ``✓ explorer · security audit  Done (20 tool uses · 41k tokens ·
        1m 30s)``, then the start of its report (or why it failed) under it."""
        self.flush_helper(agent)
        utf = self._utf()
        if skipped:
            mark, word, style = ("–" if utf else "-"), "Skipped", "dim"
        elif not ok:
            mark, word, style = ("✗" if utf else "x"), "Failed", "red"
        elif partial:
            mark, word, style = ("◐" if utf else "~"), "Partial (ran out of steps)", "yellow"
        else:
            mark, word, style = ("✓" if utf else "+"), "Done", "green"
        line = Text(f"{mark} ", style=f"bold {style}")
        line.append(f"{_short(agent, MAX_HELPER_CHARS)} · {_short(label or agent, 40)}", style="bold")
        stats = ""
        if status is not None:
            try:
                stats = f" ({step_stats(status.snapshot())})"
            except Exception:
                stats = ""
        line.append(f"  {word}{stats}", style=style if style != "green" else "dim")
        _print(self.console, line)
        report = _short(report_line(text), 160) or "(no report)"
        _print(self.console, Text(f"  {elbow(self.console)} {report}", style="red" if not ok and not skipped else "dim"))

    def team_log(self, statuses: Sequence[Any]) -> None:
        """``/team log``: every call each step of the last team job made, with a one-line result."""
        for status in statuses:
            try:
                snapshot = status.snapshot()
            except Exception:
                continue
            head = Text(f"{bullet(self.console)} {_short(status.helper, MAX_HELPER_CHARS)} · {_short(status.label, 40)}",
                        style="bold")
            head.append(f"  ({step_stats(snapshot)})", style="dim")
            _print(self.console, head)
            for name, args, summary, failed in snapshot["log"]:
                try:
                    subject = _short(self._relative(subject_of(str(name), args if isinstance(args, dict) else {})))
                except Exception:
                    subject = ""
                line = Text(f"    {_short(name, 40)}", style="bold")
                if subject:
                    line.append(f"  {subject}", style="dim")
                _print(self.console, line)
                if summary:
                    _print(self.console, Text(f"      {elbow(self.console)} {_short(summary, 140)}",
                                              style="red" if failed else "dim"))

    # -- the main chat's look-around flow ------------------------------------------------------------------------

    def flow_open(self) -> bool:
        return bool(self._flow)

    def skills_line(self) -> str:
        """The skills read in the open flow, in order: ``using-superpowers → yt-script (+ score.py)``."""
        return " → ".join(self._skills_text(item) for item in self._flow if item["tool"] == SKILL_TOOL)

    @staticmethod
    def _skills_text(item: Dict[str, Any]) -> str:
        return " → ".join(skill + (f" (+ {', '.join(files)})" if files else "")
                          for skill, files in item["skills"].items())

    def _add_to_flow(self, name: str, args: Any) -> None:
        if isinstance(args, dict) and isinstance(args.get("Input"), dict) and not ({"name", "path"} & set(args)):
            args = args["Input"]                     # text-mode calls wrap the arguments
        args = args if isinstance(args, dict) else {}
        if name == SKILL_TOOL:                       # skill reads in a row are one step: Skills  a → b
            last = self._flow[-1] if self._flow else None
            if last is None or last["tool"] != SKILL_TOOL:
                last = {"tool": SKILL_TOOL, "name": "Skills", "skills": {}, "subject": "", "result": None,
                        "failed": False}
                self._flow.append(last)
            files = last["skills"].setdefault(_short(args.get("name") or "?", 60), [])
            extra = _short(args.get("file") or "", 40)
            if extra and extra != "SKILL.md" and extra not in files:
                files.append(extra)
            last["result"] = None
            return
        try:
            subject = _short(self._relative(subject_of(name, args)))
        except Exception:
            subject = ""
        self._flow.append({"tool": name, "name": _short(name), "subject": subject, "result": None, "failed": False})

    def _flow_result(self, name: str, result: Any, is_error: bool) -> bool:
        """Put a result under its call in the open flow; False when no call there waits for it."""
        item = next((item for item in reversed(self._flow) if item["tool"] == name and item["result"] is None), None)
        if item is None:
            return False
        try:
            raw = str(_nothing_if_none(result))
        except Exception:                            # an object that cannot be shown must not stop the screen
            raw = ""
        text = plain(raw, keep_newlines=True).lstrip()
        failed = is_error or text.startswith(("ERROR", "Refused", "The user declined"))
        if failed:
            item["result"], item["failed"] = _short((_lines(text) or [""])[0], 160) or "(failed)", True
        elif name == SKILL_TOOL:
            item["result"] = ""                     # a skill's text is never shown
        else:
            summary, failed_command = summarize_result(name, raw)
            item["result"], item["failed"] = plain(summary), failed_command
        return True

    def _flow_lines(self, live: bool, now: Optional[float] = None) -> List[Text]:
        """The flow as a tree: the first step after a dot, the others hanging off it, each with its result under it.
        While it runs (``live``) the dot is yellow and the running step white, both blinking (bright, then dim, each
        half second of ``now``); once printed the dot is green, and red when the last step failed."""
        utf = self._utf()
        lit = int((time.monotonic() if now is None else now) * 2) % 2 == 0
        lines: List[Text] = []
        for index, item in enumerate(self._flow):
            running = live and item["result"] is None
            name_style = ("bold bright_white" if lit else "dim") if running else "bold"
            subject_style = ("bright_white" if lit else "dim") if running else "dim"
            if index == 0:
                if live:
                    dot = "bold yellow" if lit else "dim yellow"
                else:
                    dot = "bold red" if self._flow[-1]["failed"] else "bold green"
                line = Text()
                line.append(f"{bullet(self.console)} ", style=dot)
                indent = "  "
            else:
                last = index == len(self._flow) - 1
                line = Text(("└─ " if last else "├─ ") if utf else ("`- " if last else "|- "), style="dim")
                indent = ("   " if last else "│  ") if utf else ("   " if last else "|  ")
            line.append(item["name"], style=name_style)
            subject = self._skills_text(item) if item["tool"] == SKILL_TOOL else item["subject"]
            if subject:
                line.append(f"  {subject}", style=subject_style)
            lines.append(line)
            if item["result"]:
                lines.append(Text(f"{indent}{elbow(self.console)} {item['result']}",
                                  style="red" if item["failed"] else "dim"))
        return lines

    def flow_block(self) -> Optional[Group]:
        """The open flow, for the block above the spinner; None when none is open."""
        return Group(*self._flow_lines(live=True)) if self._flow else None

    def flush(self) -> None:
        """Print the open flow for good, then the notes held back meanwhile."""
        if self._flow:
            for line in self._flow_lines(live=False):
                _print(self.console, line)
            self._flow = []
        held, self._held = self._held, []
        for text in held:
            _print(self.console, Text(text, style="dim italic"))

    def show_event(self, event: Dict[str, Any]) -> None:
        """Draw one event. Reads, searches, listings, web calls, recalls and skill reads in a row are one flow (see
        ``_flow_lines``), drawn in place by the app (``flow_block``) and printed when the agent does something else.
        A note the agent writes is held until the next event: before a look-around step or the answer it is
        narration and is dropped; before an edit or a command it is printed."""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        try:
            name = str(event.get("name") or "")
        except Exception:                            # a name that cannot be shown must not stop the screen
            name = "?"
        if kind == "thought":
            text = plain(_nothing_if_none(event.get("content")), keep_newlines=True).strip()
            if text:
                self._held.append(text)
            return
        if (kind == "tool_call" and name in HELPER_LOOKS) or kind == "final":
            self._held = []                          # narration leading into a look-around step or the answer
        if kind == "tool_call" and name in HELPER_LOOKS:
            self._add_to_flow(name, event.get("args"))
            return
        if kind == "tool_result" and name in HELPER_LOOKS and \
                self._flow_result(name, event.get("result"), bool(event.get("is_error"))):
            return
        self.flush()
        if kind == "tool_call":
            args = event.get("args")
            try:
                summary = _short(self._relative(subject_of(name, args if isinstance(args, dict) else {})))
            except Exception:
                summary = ""
            line = Text(f"{bullet(self.console)} {_short(name)}", style="bold")
            if summary:
                line.append(f"  {summary}", style="dim")
            _print(self.console, line)
        elif kind == "tool_result":
            self._result(event.get("result"), bool(event.get("is_error")), name)
        elif kind == "final":
            content = plain(_nothing_if_none(event.get("content")), keep_newlines=True)
            _print(self.console, Markdown(content, hyperlinks=False))

    def _result(self, result: Any, is_error: bool, name: str = "") -> None:
        """One line under the call (``⎿ Read 51 lines``); a failure shows its first lines in red instead."""
        try:
            raw = str(_nothing_if_none(result))
        except Exception:                            # an object that cannot be shown must not stop the screen
            raw = ""
        text = plain(raw, keep_newlines=True)
        failed = is_error or text.lstrip().startswith(("ERROR", "Refused", "The user declined"))
        if not failed:
            summary, failed_command = summarize_result(name, raw)        # counted on the raw text, shown escaped
            line = Text(f"  {elbow(self.console)} {plain(summary)}", style="red" if failed_command else "dim")
            _print(self.console, line)
            return
        style = "red"
        lines = _lines(text)
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
        """The welcome box; in full-screen mode (``header`` set) it becomes the fixed header instead."""
        if self.header is not None:
            compact = " · ".join(str(part) for part in (f"agentx v{parts.get('version', '')}", parts.get("model"),
                                                        parts.get("folder")) if part)
            self.header(lambda width: welcome_panel(width=width, **parts), compact)
            return
        _print(self.console, welcome_panel(width=self.console.width, **parts))

    def error(self, text: str) -> None:
        _print(self.console, Text(plain(text, keep_newlines=True), style="bold red"))

    def team_plan(self, steps: Any) -> None:
        _print(self.console, Text("Team plan:", style="bold magenta"))
        for number, step in enumerate(steps if isinstance(steps, (list, tuple)) else [], 1):
            if not isinstance(step, dict):
                continue
            label = f" · {_short(step.get('label'), 40)}" if step.get("label") else ""
            new = step.get("new")
            made = (f" (new helper; tools: {_short(', '.join(str(t) for t in new), 120) or 'none'})"
                    if isinstance(new, list) else "")
            _print(self.console, Text(f"  {number}. {_short(step.get('agent'), MAX_HELPER_CHARS)}{made}{label}: "
                                      f"{_short(step.get('query'), 160)}", style="dim"))

    def team_dispatch(self, agent: Any, query: Any) -> None:
        _print(self.console, Text(f"-> {_short(agent, MAX_HELPER_CHARS)}: {_short(query, 200)}",
                                  style="bold magenta"))

    def team_result(self, agent: Any, ok: bool, text: Any, partial: bool = False) -> None:
        self.flush_helper(agent)
        report = _short(_nothing_if_none(text), 160) or "(no report)"
        mark = ("partial" if partial else "done") if ok else "failed"
        _print(self.console, Text(f"   {mark} ({_short(agent, MAX_HELPER_CHARS)}): {report}",
                                  style="dim" if ok else "red"))

    def helper_call(self, helper: Any, name: Any, args: Any) -> None:
        """A helper's tool call. A looking call is counted into the helper's line (``helpers_line``); any other call
        prints that line first, then its own, drawn like the main chat's with the helper's name in front."""
        args = args if isinstance(args, dict) else {}
        if isinstance(args.get("Input"), dict) and "path" not in args and "name" not in args:
            args = args["Input"]                     # text-mode calls wrap the arguments
        kind = HELPER_LOOKS.get(str(name))
        if kind is not None:
            counts = self._helpers.setdefault(_short(helper, MAX_HELPER_CHARS), {})
            if kind == "read":
                counts.setdefault("read", {})[str(args.get("path", ""))] = None
            elif kind == "skill":
                skills = counts.setdefault("skill", {})
                skills[_short(args.get("name") or "?", 60)] = None
            else:
                counts[kind] = counts.get(kind, 0) + 1
            return
        self.flush_helper(helper)
        try:
            summary = _short(self._relative(subject_of(str(name), args)))
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


def is_terminal() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


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
