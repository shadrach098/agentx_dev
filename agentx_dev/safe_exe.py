"""Find helper programs without looking in the current folder.

On Windows, ``shutil.which`` and ``CreateProcess`` both search the current
folder before PATH. agentx runs inside the user's project, which may be a
cloned repo, so a ``tailscale.cmd`` or ``bash.exe`` left at its root would be
started in place of the real program. These helpers search only absolute PATH
entries outside the current folder, and name Windows' own tools by their full
System32 path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _extensions(name: str) -> list[str]:
    if sys.platform != "win32":
        return [""]
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    if os.path.splitext(name)[1].upper() in {e.upper() for e in exts}:
        return [""]
    return exts


def which(name: str, cwd: str | os.PathLike[str] | None = None) -> str | None:
    """Return the absolute path of ``name`` found on PATH, or None.

    Unlike ``shutil.which`` this never returns a program from the current
    folder (or ``cwd``, or anywhere inside it), and skips relative or empty
    PATH entries. A name that already contains a folder is not looked up.
    """
    if not name or os.path.dirname(name):
        return None
    here = Path(cwd if cwd is not None else os.getcwd()).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        try:
            folder = Path(entry).resolve()
        except OSError:
            continue
        if folder == here or _inside(folder, here):
            continue
        for ext in _extensions(name):
            candidate = folder / (name + ext)
            if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def system32(name: str) -> str:
    """The full path of a Windows tool in System32, such as ``taskkill.exe``.

    Falls back to the bare name off Windows, where it is never used.
    """
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    if sys.platform != "win32":
        return name
    return os.path.join(root, "System32", name)


def powershell() -> str:
    """Windows PowerShell by its full path (the bare name searches the current folder first)."""
    return system32(os.path.join("WindowsPowerShell", "v1.0", "powershell.exe"))
