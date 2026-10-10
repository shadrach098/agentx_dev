"""The text an approval prompt shows: a diff for edits, the command, the code, the paths.

Building a preview must never raise and must stay cheap: it reads only regular
files up to a size cap, never expands ``~`` (the tools do not), and bounds every
diff and body it returns.
"""

from __future__ import annotations

import difflib
import json
import os
import stat
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Mapping, Optional

MAX_PREVIEW_LINES = 200
MAX_READ_BYTES = 1_000_000        # larger existing files are not read for a preview
MAX_CONTENT_CHARS = 1_000_000     # larger new content is not diffed
MAX_DIFF_LINES = 20_000           # more lines than this on either side is not diffed
MAX_LINE_CHARS = 2_000
# A command, a piece of code or an unknown tool's arguments are shown WHOLE up to this many characters, never
# cut short (a dangerous tail must not hide behind a cut). Past it the preview is cut with a note saying how
# much is hidden and ``Preview.truncated`` is set; the prompt then declines the action unseen.
MAX_COMMAND_CHARS = 20_000


@dataclass(frozen=True)
class Preview:
    title: str
    body: str
    kind: str          # "diff" | "command" | "code" | "text"
    truncated: bool = False    # a command / code / argument text was cut at MAX_COMMAND_CHARS


@dataclass(frozen=True)
class _Disk:
    """What is at a path: ``missing``, ``other`` (not a regular file or unreadable),
    ``large``, ``binary`` or ``ok`` (then ``text`` holds the content)."""
    state: str
    text: str = ""
    size: int = 0
    valid_utf8: bool = True


def _resolve(path: str, cwd: Path) -> Optional[Path]:
    """``path`` against ``cwd`` as the tools do: no ``~`` expansion. None if unusable."""
    try:
        target = Path(path)
        return target if target.is_absolute() else Path(cwd) / target
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _read(path: Optional[Path]) -> _Disk:
    if path is None:
        return _Disk("missing")
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError, ValueError, RuntimeError):
        return _Disk("missing")     # includes an embedded NUL in the name
    except OSError:
        return _Disk("other")
    if not stat.S_ISREG(info.st_mode):
        return _Disk("other")       # a directory, FIFO, device, socket
    if info.st_size > MAX_READ_BYTES:
        return _Disk("large", size=info.st_size)
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_READ_BYTES + 1)
    except (OSError, ValueError, RuntimeError):
        return _Disk("other")
    if len(data) > MAX_READ_BYTES:
        return _Disk("large", size=len(data))
    if b"\x00" in data[:8192]:
        return _Disk("binary", size=len(data))
    try:
        data.decode("utf-8")
        valid = True
    except UnicodeDecodeError:
        valid = False
    return _Disk("ok", data.decode("utf-8", errors="replace"), len(data), valid)


def _cut_line(line: str) -> str:
    """One line bounded to MAX_LINE_CHARS; a cut says how many characters it hides."""
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]} ... [{len(line) - MAX_LINE_CHARS} more characters hidden]"


def _whole(text: str) -> tuple:
    """``(body, truncated)``: the text whole up to MAX_COMMAND_CHARS, else cut there with a note."""
    if len(text) <= MAX_COMMAND_CHARS:
        return text, False
    return f"{text[:MAX_COMMAND_CHARS]}\n... [{len(text) - MAX_COMMAND_CHARS} more characters hidden]", True


def _diff(old: str, new: str, shown: str) -> str:
    if old == new:
        return "(no change)"
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if old_lines == new_lines:
        return "(only line endings or the final newline change)"
    stream = difflib.unified_diff(old_lines, new_lines,
                                  fromfile=f"a/{shown}", tofile=f"b/{shown}", lineterm="")
    lines = list(islice(stream, MAX_PREVIEW_LINES + 1))
    if len(lines) > MAX_PREVIEW_LINES:
        extra = 1 + sum(1 for _ in stream)
        lines = lines[:MAX_PREVIEW_LINES] + [f"... {extra} more lines"]
    return "\n".join(_cut_line(line) for line in lines)


def _diff_preview(title: str, old: str, new: str, shown: str) -> Preview:
    if len(old) > MAX_CONTENT_CHARS or len(new) > MAX_CONTENT_CHARS:
        return Preview(title, f"(content too large to preview: {max(len(old), len(new))} characters)", "text")
    if old.count("\n") > MAX_DIFF_LINES or new.count("\n") > MAX_DIFF_LINES:
        return Preview(title, "(too many lines to preview as a diff)", "text")
    return Preview(title, _diff(old, new, shown), "diff")


def _fresh_name(target: Path) -> str:
    """The name write_file's rename mode would pick (report.md -> report_1.md), no side effects."""
    for i in range(1, 1000):
        candidate = target.parent / f"{target.stem}_{i}{target.suffix}"
        try:
            if not candidate.exists():
                return candidate.name
        except (OSError, ValueError):
            break
    return "a numbered copy"


def _unescape_json_escapes(s: str) -> str:
    """Mirror of DefaultTools._maybe_unescape_json_escapes (applied to edit_file's find/replace)."""
    out = s
    for literal, real in (("\\n", "\n"), ("\\t", "\t"), ("\\r", "\r")):
        if literal in out and real not in out:
            out = out.replace(literal, real)
    return out


def _write_file(args: Mapping[str, Any], cwd: Path) -> Preview:
    shown = str(args.get("path", ""))
    content = str(args.get("content", ""))
    raw_mode = args.get("if_exists", "refuse")
    mode = str(raw_mode or "refuse").lower().strip()
    if mode not in {"refuse", "rename", "overwrite", "append"}:
        return Preview(f"Write {shown}", f"(if_exists={raw_mode!r} is not valid; the write will be refused)", "text")
    target = _resolve(shown, cwd)
    disk = _read(target)
    if disk.state == "missing":
        return _diff_preview(f"Create {shown}", "", content, shown)
    if mode == "rename":
        fresh = _fresh_name(target) if target is not None else "a numbered copy"
        title = f"Write {shown} (saved under a new name, {fresh}, since it exists)"
        return _diff_preview(title, "", content, shown)
    if disk.state == "other":
        return _diff_preview(f"Write {shown} (the target is not a regular file)", "", content, shown)
    if mode == "append":
        old = disk.text if disk.state == "ok" else ""
        return _diff_preview(f"Append to {shown}", old, old + content, shown)
    if disk.state == "large":
        return Preview(f"Write {shown}", f"(file too large to preview: {disk.size} bytes)", "text")
    if disk.state == "binary":
        return Preview(f"Write {shown}", "(the existing file is binary; no preview)", "text")
    if mode == "overwrite":
        return _diff_preview(f"Overwrite {shown}", disk.text, content, shown)
    title = f"Write {shown} (already exists; refused unless it was read this session)"
    return _diff_preview(title, disk.text, content, shown)


def _edit_file(args: Mapping[str, Any], cwd: Path) -> Preview:
    shown = str(args.get("path", ""))
    title = f"Edit {shown}"
    disk = _read(_resolve(shown, cwd))
    if disk.state == "missing":
        return Preview(title, "(file not found)", "text")
    if disk.state == "other":
        return Preview(title, "(not a regular file; the edit will fail)", "text")
    if disk.state == "large":
        return Preview(title, f"(file too large to preview: {disk.size} bytes)", "text")
    if disk.state == "binary" or not disk.valid_utf8:
        return Preview(title, "(the file is not valid UTF-8 text; the edit will fail)", "text")
    find = _unescape_json_escapes(str(args.get("find", "")))
    replace = _unescape_json_escapes(str(args.get("replace", "")))
    # The tool reads with Path.read_text, which turns \r\n and \r into \n.
    text = disk.text.replace("\r\n", "\n").replace("\r", "\n")
    count = text.count(find)
    if count == 0:
        return Preview(title, "(the text to replace was not found)", "text")
    if count > 1:
        return Preview(title, f"(text appears {count} times; the edit will be refused)", "text")
    return _diff_preview(title, text, text.replace(find, replace, 1), shown)


def _generic(tool: Any, args: Any) -> Preview:
    try:
        title = str(tool)
    except Exception:
        title = "tool call"
    try:
        body = json.dumps(dict(args), default=str, indent=2)
    except Exception:
        try:
            body = repr(args)
        except Exception:
            body = "(arguments cannot be shown)"
    body, truncated = _whole(body)
    return Preview(title, body, "text", truncated)


def _build(tool: str, args: Mapping[str, Any], cwd: Path) -> Preview:
    if not isinstance(args, Mapping):
        return _generic(tool, args)
    if tool == "write_file":
        return _write_file(args, cwd)
    if tool == "edit_file":
        return _edit_file(args, cwd)
    if tool == "run_shell":
        where = f" in {args['cwd']}" if args.get("cwd") else ""
        body, truncated = _whole(str(args.get("command", "")))
        return Preview(f"Run shell command{where}", body, "command", truncated)
    if tool == "run_python":
        body, truncated = _whole(str(args.get("code", "")))
        return Preview("Run Python", body, "code", truncated)
    if tool == "delete_path":
        extra = " and everything inside it" if args.get("recursive") else ""
        return Preview(f"Delete {args.get('path', '')}{extra}", "", "text")
    if tool == "move_file":
        return Preview(f"Move {args.get('source', '')} to {args.get('destination', '')}", "", "text")
    return _generic(tool, args)


def preview_for(tool: str, args: Mapping[str, Any], cwd: Path) -> Preview:
    """What to show the user before ``tool`` runs with ``args`` in ``cwd``. Never raises."""
    try:
        return _build(tool, args, cwd)
    except Exception:
        return _generic(tool, args)
