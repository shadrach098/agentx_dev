"""How tool results and team steps read on the screen: ``plain`` (defuses control characters and escapes), result summaries, and the short token, time and step texts."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Tuple

from rich.console import Console



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


def elbow(console: Console) -> str:
    """The marker in front of a tool's result; a plain one on a console that cannot print it."""
    return "⎿" if "utf" in (console.encoding or "").lower() else "->"


_COUNT = re.compile(r"(\d+) match\(es\)")


_SCANNED = re.compile(r"across (\d+) scanned file")


_NUMBERED = re.compile(r"(\d+)\t")


_OF_TOTAL = re.compile(r"\[Showing lines (\d+)-(\d+) of (\d+)")


_EXIT = re.compile(r"^\(exit code (-?\d+)\)$")


_PICTURE = re.compile(r"^Picture .*?: (?:PNG|JPEG|GIF|WebP)(?:, (\d+x\d+))?, ")


_CAUTIONS = ("[Web content:", "[MCP result from", "These notes were written by agentx", "[This skill came from")


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _first_and_more(lines: List[str]) -> str:
    """The first line, cut to fit, and how many follow."""
    if not lines:
        return "No output"
    more = len(lines) - 1
    return _short(lines[0], 120) + (f"  (+{more:,} more line{'' if more == 1 else 's'})" if more else "")


def _command_output(lines: List[str]) -> Tuple[List[str], Optional[int]]:
    """``(output lines, exit code)`` of a run_shell / run_python result: its ``$ ...`` header and the
    ``--- stdout ---`` / ``--- stderr ---`` markers left out."""
    code: Optional[int] = None
    out: List[str] = []
    for index, line in enumerate(lines):
        if index == 0 and line.startswith("$ ("):
            continue
        if line in ("--- stdout ---", "--- stderr ---"):
            continue
        found = _EXIT.match(line.strip())
        if found:
            code = int(found.group(1))
            continue
        out.append(line)
    return out, code


def summarize_result(name: str, text: str) -> Tuple[str, bool]:
    """``(one line about what a tool returned, whether it failed)``. Read: how much; search: how many; a
    command: its first line of output and how many more, or the exit code and the last line when it failed;
    anything else: its first line and how many more."""
    every = text.replace("\r\n", "\n").split("\n")       # only real line breaks; \x0b, \x85 and the like stay in
    raw = [line for line in every if line.strip()]
    while raw and raw[0].lstrip().startswith(_CAUTIONS):          # the notes put before outside data
        raw = raw[1:]
    if not raw:
        return "No output", False
    first = raw[0].strip()
    if name == "read_path":
        picture = _PICTURE.match(first)
        if picture:
            return "Picture" + (f", {picture.group(1)}" if picture.group(1) else ""), False
        shown = _OF_TOTAL.search(text)
        if shown:
            return f"Read lines {int(shown.group(1)):,}-{int(shown.group(2)):,} of {int(shown.group(3)):,}", False
        numbered = [m for m in (_NUMBERED.match(line) for line in raw) if m]
        if numbered and len(numbered) == len(raw):
            return f"Read lines {int(numbered[0].group(1)):,}-{int(numbered[-1].group(1)):,}", False
        return f"Read {_plural(len(every) - (1 if every and every[-1] == '' else 0), 'line')}", False
    if name == "list_directory":
        entries = [line for line in raw if line.lstrip().startswith(("[FILE]", "[DIR]"))]
        return (f"Listed {_plural(len(entries), 'entry').replace('entrys', 'entries')}" if entries
                else _short(first, 120)), False
    if name in ("find_files", "grep"):
        count = _COUNT.search(first)
        if count is None:
            return ("No matches" if first.startswith("(no matches") else _short(first, 120)), False
        found = int(count.group(1))
        cut = "+" if "truncated" in first else ""
        if name == "find_files":
            return f"Found {found:,}{cut} file{'' if found == 1 else 's'}", False
        files = _SCANNED.search(first)
        where = f" in {_plural(int(files.group(1)), 'file')} searched" if files else ""
        return f"{found:,}{cut} match{'' if found == 1 else 'es'}{where}", False
    if name in ("run_shell", "run_python"):
        out, code = _command_output(raw)
        if code not in (None, 0):
            last = _short(out[-1], 120) if out else ""
            return f"Exit code {code}" + (f": {last}" if last else ""), True
        return _first_and_more(out), False
    if name == "recall":
        notes = [line for line in raw if line.lstrip().startswith("- ")]
        return (f"Found {_plural(len(notes), 'note')}" if notes else _short(first, 120)), False
    if name == "web_search":
        hits = [line for line in raw if re.match(r"\s*\d+\. ", line)]
        return (f"Found {_plural(len(hits), 'result')}" if hits else _short(first, 120)), False
    if name == "web_fetch":
        body = "\n".join(raw)
        return f"Fetched {len(body):,} characters", False
    return _first_and_more([line.strip() for line in raw]), False


def _short(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    flat = " ".join(plain(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


ACCENT = "#b8ff3e"


SKILL_TOOL = "use_skill"


# A team helper's calls that only look: counted into one line per helper instead of a line each.
HELPER_LOOKS = {"read_path": "read", "list_directory": "list", "find_files": "search", "grep": "search",
                "web_search": "web", "web_fetch": "fetch", "recall": "recall", SKILL_TOOL: "skill"}


STEP_KINDS = {**HELPER_LOOKS, "run_shell": "command", "run_python": "command", "write_file": "write",
              "edit_file": "edit"}


def _times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count:,} times")


def helper_activity(counts: Mapping[str, Any], sep: str = " · ") -> str:
    """``Read 12 files · Searched 3 patterns · Skills exploring-a-project`` for one helper's looking calls."""
    parts = []
    if counts.get("read"):
        parts.append(f"Read {_plural(len(counts['read']), 'file')}")
    if counts.get("search"):
        parts.append(f"Searched {_plural(counts['search'], 'pattern')}")
    if counts.get("list"):
        parts.append(f"Listed {_plural(counts['list'], 'folder')}")
    if counts.get("web"):
        parts.append(f"Searched the web {_times(counts['web'])}")
    if counts.get("fetch"):
        parts.append(f"Fetched {_plural(counts['fetch'], 'page')}")
    if counts.get("recall"):
        parts.append(f"Looked up notes {_times(counts['recall'])}")
    if counts.get("command"):
        parts.append(f"Ran {_plural(counts['command'], 'command')}")
    if counts.get("edit"):
        parts.append(f"Made {_plural(counts['edit'], 'edit')}")
    if counts.get("write"):
        parts.append(f"Wrote {_plural(counts['write'], 'file')}")
    if counts.get("other"):
        parts.append(f"Used other tools {_times(counts['other'])}")
    if counts.get("skill"):
        parts.append("Skills " + ", ".join(counts["skill"]))
    return sep.join(parts)


def step_counts(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """``helper_activity``'s counts from a ``StepStatus.snapshot()``."""
    counts: Dict[str, Any] = {"read": dict(snapshot.get("files") or {}),
                              "skill": {name: None for name in snapshot.get("skills") or []}}
    for name, number in (snapshot.get("calls") or {}).items():
        kind = STEP_KINDS.get(str(name), "other")
        if kind != "read":
            counts[kind] = counts.get(kind, 0) + int(number)
    return counts


def short_tokens(count: int) -> str:
    count = max(0, int(count or 0))
    if count < 1000:
        return f"{count}"
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}m"


def short_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds or 0)))     # 89.9999 is 1m 30s, not 1m 29s
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def token_text(snapshot: Mapping[str, Any]) -> str:
    """``230k tokens, 80% cached`` (the cached share only when the provider reported one)."""
    tokens, cached = int(snapshot.get("tokens") or 0), int(snapshot.get("cached") or 0)
    share = f", {cached * 100 // tokens}% cached" if tokens and cached else ""
    return f"{short_tokens(tokens)} tokens{share}"


def step_stats(snapshot: Mapping[str, Any]) -> str:
    uses = int(snapshot.get("tool_uses") or 0)
    return f"{_plural(uses, 'tool use')} · {token_text(snapshot)} · {short_time(snapshot.get('seconds') or 0)}"


_MARKDOWN = re.compile(r"(^|\s)#{1,6}\s+|\*\*|__|`")


def report_line(text: Any) -> str:
    """The start of a report as one plain line: markdown headings, bold and code marks taken out."""
    flat = " ".join(str(_nothing_if_none(text)).split())
    return " ".join(_MARKDOWN.sub(" ", flat).split())


def _nothing_if_none(value: Any) -> Any:
    return "" if value is None else value
