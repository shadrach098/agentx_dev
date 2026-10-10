"""Project instructions (AGENTX.md) and the system addendum the runner is given.

The instruction files may come from a repository the user just cloned, so reading them never raises, is bounded in
size, skips binary files, and never follows a symlink that leads out of the folder the file sits in. What a file
says is placed in the prompt as text; it is the user's own ``~/.agentx/AGENTX.md`` and the base prompt that set the
rules, not a cloned repository.
"""

from __future__ import annotations

import os
import platform
import stat
from pathlib import Path
from typing import List, Optional

MAX_CONTEXT_CHARS = 20000
FILE_NAME = "AGENTX.md"
_READ_BYTES = MAX_CONTEXT_CHARS * 4 + 1          # enough for MAX_CONTEXT_CHARS characters of UTF-8, plus one to see a cut

BASE_PROMPT = """You are agentx, a coding assistant working in the user's terminal on their project.

Working folder: {cwd}
Operating system: {system}

How to work:
- Use your tools to look at the project before you change it: list directories, find files, grep, and read a file before you edit it.
- Make the smallest change that does what was asked, and keep the existing style.
- Edits and commands are shown to the user for approval; if a request is declined, do not retry the same thing, adjust or ask what they want.
- After changing code, run the project's tests or a quick check when you can, and say what you ran and what it showed.
- Be brief in your answers: say what you did and what to look at."""


def _resolve(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(path))


def _clean(text: object) -> str:
    """Text made safe to put on a heading line: control characters (newlines, escapes) become '?'."""
    return "".join(c if c.isprintable() else "?" for c in str(text))


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _stays_inside(path: Path, folder: Path) -> bool:
    """False for a symlink whose target is outside ``folder``; a plain file is always fine."""
    try:
        if not path.is_symlink():
            return True
        path.resolve().relative_to(folder)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def find_context_files(cwd: Path, home: Path) -> List[Path]:
    """The AGENTX.md files that apply: ``~/.agentx/AGENTX.md`` first, then the working folder's ancestors from the
    top down to the working folder (nearest last). A symlinked file that points outside its own folder is left out."""
    found: List[Path] = []
    user_file = Path(home) / ".agentx" / FILE_NAME
    if _is_file(user_file):
        found.append(user_file)
    folder = _resolve(cwd)
    for directory in reversed([folder, *folder.parents]):
        candidate = directory / FILE_NAME
        if _is_file(candidate) and _stays_inside(candidate, directory):
            found.append(candidate)
    return found


def _read(path: Path) -> Optional[str]:
    """The start of a text file, or None when it is not a readable regular text file."""
    try:
        with open(path, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            data = handle.read(_READ_BYTES)
    except (OSError, ValueError):
        return None
    if b"\x00" in data:                                          # binary, not instructions
        return None
    return data.decode("utf-8", errors="replace").strip()


def build_addendum(cwd: Path, home: Path, *, system: Optional[str] = None) -> str:
    """The base prompt plus the project instructions, cut at ``MAX_CONTEXT_CHARS`` keeping the nearest files."""
    base = BASE_PROMPT.format(cwd=_clean(cwd), system=_clean(system or platform.system() or "unknown"))
    sections: List[str] = []
    used = 0
    for path in reversed(find_context_files(cwd, home)):          # nearest first
        text = _read(path)
        if not text:
            continue
        room = MAX_CONTEXT_CHARS - used
        if room <= 0:
            break
        if len(text) > room:
            if sections:                                         # a farther file that does not fit is dropped
                break
            text = text[:room] + "\n(truncated)"
            used += room
        else:
            used += len(text)
        sections.append(f"## {_clean(path)}\n{text}")
    if not sections:
        return base
    return base + "\n\nProject instructions (AGENTX.md files, the nearest last):\n\n" + "\n\n".join(reversed(sections))
