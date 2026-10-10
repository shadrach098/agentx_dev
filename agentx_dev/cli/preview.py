"""The text an approval prompt shows: a diff for edits, the command, the code, the paths."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

MAX_PREVIEW_LINES = 200


@dataclass(frozen=True)
class Preview:
    title: str
    body: str
    kind: str          # "diff" | "command" | "code" | "text"


def _resolve(path: str, cwd: Path) -> Path:
    target = Path(path).expanduser()
    return target if target.is_absolute() else Path(cwd) / target


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:                      # missing, a directory, unreadable
        return None


def _diff(old: str, new: str, shown: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile=f"a/{shown}", tofile=f"b/{shown}", lineterm=""))
    if not lines:
        return "(no change)"
    if len(lines) > MAX_PREVIEW_LINES:
        extra = len(lines) - MAX_PREVIEW_LINES
        lines = lines[:MAX_PREVIEW_LINES] + [f"... {extra} more lines"]
    return "\n".join(lines)


def preview_for(tool: str, args: Mapping[str, Any], cwd: Path) -> Preview:
    """What to show the user before ``tool`` runs with ``args`` in ``cwd``."""
    if tool == "write_file":
        shown = str(args.get("path", ""))
        content = str(args.get("content", ""))
        mode = str(args.get("if_exists", "refuse"))
        old = _read(_resolve(shown, cwd))
        if old is None:
            title, before, after = f"Create {shown}", "", content
        elif mode == "append":
            title, before, after = f"Append to {shown}", old, old + content
        elif mode == "rename":
            title, before, after = f"Write {shown} (saved under a new name, since it exists)", "", content
        elif mode == "overwrite":
            title, before, after = f"Overwrite {shown}", old, content
        else:
            title, before, after = f"Write {shown}", old, content
        return Preview(title, _diff(before, after, shown), "diff")
    if tool == "edit_file":
        shown = str(args.get("path", ""))
        old = _read(_resolve(shown, cwd))
        if old is None:
            return Preview(f"Edit {shown}", "(file not found)", "text")
        find = str(args.get("find", ""))
        if not find or find not in old:
            return Preview(f"Edit {shown}", "(the text to replace was not found)", "text")
        new = old.replace(find, str(args.get("replace", "")), 1)
        return Preview(f"Edit {shown}", _diff(old, new, shown), "diff")
    if tool == "run_shell":
        where = f" in {args['cwd']}" if args.get("cwd") else ""
        return Preview(f"Run shell command{where}", str(args.get("command", "")), "command")
    if tool == "run_python":
        return Preview("Run Python", str(args.get("code", "")), "code")
    if tool == "delete_path":
        extra = " and everything inside it" if args.get("recursive") else ""
        return Preview(f"Delete {args.get('path', '')}{extra}", "", "text")
    if tool == "move_file":
        return Preview(f"Move {args.get('source', '')} to {args.get('destination', '')}", "", "text")
    return Preview(tool, json.dumps(dict(args), default=str, indent=2)[:2000], "text")
