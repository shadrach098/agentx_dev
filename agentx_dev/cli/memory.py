"""The work log kept in a project folder: ``<folder>/.agentx/log.md``.

The app writes it, from the events of a finished turn, with no model call; the model cannot (``.agentx/*`` is on
the always-on deny list and the app writes with Python, not through a tool). This module has no terminal output
and takes the redaction function as an argument, so it is easy to test. Nothing here raises on a damaged file or
an unwritable folder."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

LOG_DIR = ".agentx"
LOG_NAME = "log.md"
OLDER_NAME = "log-older.md"
MAX_LOG_BYTES = 200 * 1024            # above this the oldest half of the entries moves to log-older.md
MAX_ENTRY_CHARS = 1200
MAX_TITLE_CHARS = 80
MAX_ANSWER_CHARS = 300
MAX_COMMAND_CHARS = 100
MAX_PATHS = 10
MAX_SUMMARY_ENTRY_CHARS = 1500
READ_BYTES = 24 * 1024                # how much of the end of the log is looked at
MAX_LOADED_CHARS = 6000               # how much of it goes into the prompt

CHANGING_TOOLS = ("write_file", "edit_file", "delete_path")
REFUSAL_STARTS = ("Refused", "The user declined", "ERROR", "Error")

Redact = Callable[[str], str]


def log_path(cwd: Path) -> Path:
    return Path(cwd) / LOG_DIR / LOG_NAME


@dataclass
class TurnRecord:
    request: str
    answer: str = ""
    changed: List[str] = field(default_factory=list)
    ran: List[str] = field(default_factory=list)
    team: List[str] = field(default_factory=list)


def _flat(text: Any, limit: int) -> str:
    flat = " ".join(str("" if text is None else text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _unique(items: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _refused(result: Any, is_error: bool) -> bool:
    return bool(is_error) or str("" if result is None else result).lstrip().startswith(REFUSAL_STARTS)


def _note_call(record: TurnRecord, name: Any, args: Any, result: Any, is_error: bool) -> None:
    if _refused(result, is_error):
        return
    args = args if isinstance(args, dict) else {}
    if name in CHANGING_TOOLS:
        if args.get("path"):
            record.changed.append(str(args["path"]))
    elif name == "move_file":
        record.changed.append(f"{args.get('source', '')} -> {args.get('destination', '')}")
    elif name == "run_shell":
        if args.get("command"):
            record.ran.append(str(args["command"]))
    elif name == "run_python":
        lines = str(args.get("code", "")).strip().splitlines()
        if lines:
            record.ran.append("python: " + lines[0])
    elif name == "start_team_job":
        # Raw text on purpose: it is cut by ``entry_text`` only after redaction, so a key cannot straddle the cut.
        match = re.search(r"outcome: (\w+)", str(result or ""))
        record.team.append(f"{args.get('task', '')} ({match.group(1) if match else 'ended'})")


def record_from_events(request: str, events: Iterable[Dict[str, Any]]) -> TurnRecord:
    """What a finished turn did, from its stream events. Refused, declined and failed calls are not counted. This
    is a text check on the tool results, so a rare unusual result could be misjudged."""
    record = TurnRecord(request=str("" if request is None else request))
    pending: Any = None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "tool_call":
            pending = event
        elif kind == "tool_result":
            if isinstance(pending, dict) and pending.get("name") == event.get("name"):
                _note_call(record, event.get("name"), pending.get("args"), event.get("result"),
                           bool(event.get("is_error")))
            pending = None
        elif kind == "final":
            record.answer = str(event.get("content") or "")
    record.changed = _unique(record.changed)
    record.ran = _unique(record.ran)
    return record


def entry_text(record: TurnRecord, when: datetime, redact: Redact = str) -> str:
    """One log entry (no trailing newline), at most ``MAX_ENTRY_CHARS`` long. Every field is passed through
    ``redact`` and flattened to one line, so nothing in it can start a new entry."""
    def safe(text: Any, limit: int) -> str:
        return _flat(redact(str("" if text is None else text)), limit)

    lines = [f"## {when.strftime('%Y-%m-%d %H:%M')} - {safe(record.request, MAX_TITLE_CHARS) or '(no message)'}"]
    if record.changed:
        shown = ", ".join(safe(path, 200) for path in record.changed[:MAX_PATHS])
        more = len(record.changed) - MAX_PATHS
        lines.append("Changed: " + shown + (f" and {more} more" if more > 0 else ""))
    if record.ran:
        lines.append("Ran: " + "; ".join(safe(command, MAX_COMMAND_CHARS) for command in record.ran[:MAX_PATHS]))
    if record.team:
        lines.append("Team job: " + "; ".join(safe(item, 160) for item in record.team))
    answer = safe(record.answer, MAX_ANSWER_CHARS)
    if answer:
        lines.append("Answer: " + answer)
    text = "\n".join(lines)
    return text if len(text) <= MAX_ENTRY_CHARS else text[: MAX_ENTRY_CHARS - 4] + " ..."


def summary_entry_text(summary: str, when: datetime, redact: Redact = str) -> str:
    """The entry written when a conversation is summarised. Lines of the summary that start with ``#`` get a
    backslash in front, so the summary cannot forge an entry heading."""
    body = redact(str(summary or "")).strip()[:MAX_SUMMARY_ENTRY_CHARS]
    lines = [("\\" + line) if line.lstrip().startswith("#") else line for line in body.splitlines()]
    return f"## {when.strftime('%Y-%m-%d %H:%M')} - Earlier conversation summarised\n" + "\n".join(lines)


def _split_entries(text: str) -> List[str]:
    return [piece for piece in re.split(r"(?m)^(?=## )", text) if piece]


TEMP_NAME = "log.tmp"


def _owner_only(path: Path) -> None:
    """Best effort: the log is for its owner only, where the platform has file modes."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _safe_location(cwd: Path) -> bool:
    """False when ``.agentx`` or a file in it is a symlink, or leads outside the folder, so a cloned project cannot
    point the log at some other file. Never raises."""
    try:
        root = Path(cwd).resolve()
        folder = root / LOG_DIR
        for item in (folder, folder / LOG_NAME, folder / OLDER_NAME, folder / TEMP_NAME):
            if item.is_symlink():
                return False
            item.resolve().relative_to(root)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _rotate(path: Path, limit: int) -> None:
    try:
        if not _safe_location(path.parent.parent):
            return
        if path.stat().st_size <= limit:
            return
        entries = _split_entries(path.read_text(encoding="utf-8", errors="replace"))
        if len(entries) < 2:
            return
        half = len(entries) // 2
        older = path.with_name(OLDER_NAME)
        with open(older, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(entries[:half]))
        _owner_only(older)
        temporary = path.with_name(TEMP_NAME)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(entries[half:]))
        _owner_only(temporary)
        os.replace(temporary, path)
    except (OSError, ValueError):
        pass


def append_entry(cwd: Path, text: str, limit: int = MAX_LOG_BYTES) -> bool:
    """Append ``text`` as one entry, creating ``.agentx/log.md`` (readable by its owner only, where the platform
    allows) and rotating it above ``limit`` bytes. False on any failure; never raises."""
    path = log_path(cwd)
    try:
        if not _safe_location(cwd):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(str(text).rstrip("\n") + "\n\n")
        if is_new:
            _owner_only(path)
        _rotate(path, limit)
        return True
    except (OSError, ValueError):
        return False


def read_recent(cwd: Path, max_chars: int = MAX_LOADED_CHARS) -> str:
    """The newest entries of the log, oldest first, at most ``max_chars`` long, starting on an entry heading.
    Only the last ``READ_BYTES`` of the file are read. "" when the log is missing, unreadable or binary."""
    path = log_path(cwd)
    if not _safe_location(cwd):
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - READ_BYTES)
            handle.seek(start)
            raw = handle.read(READ_BYTES)
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""
    pieces = _split_entries(raw.decode("utf-8", errors="replace"))
    if start > 0 and pieces:
        pieces = pieces[1:]                       # the read began in the middle of an entry
    while pieces and not pieces[0].startswith("## "):
        pieces = pieces[1:]                       # text before the first heading is never returned
    kept: List[str] = []
    total = 0
    for piece in reversed(pieces):
        if total + len(piece) > max_chars:
            break
        kept.append(piece)
        total += len(piece)
    return "".join(reversed(kept)).strip()
