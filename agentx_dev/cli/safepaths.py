"""Writing the app's own files in ``<folder>/.agentx`` (the work log and the folder settings) safely.

A cloned project controls what is in its folder, so ``.agentx`` or a file in it may be a link to somewhere else
(a symlink, or a junction on Windows). ``safe_location`` refuses that, and ``replace_file`` writes through a new,
uniquely named temporary file, so a planted fixed-name temporary file is never opened."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

FOLDER = ".agentx"


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)          # Python 3.12+; resolve() catches it otherwise
    return bool(is_junction is not None and is_junction(path))


def safe_location(cwd: Path, *names: str) -> bool:
    """False when ``<cwd>/.agentx``, or one of the files ``names`` inside it, is a link or leads outside ``cwd``.
    Never raises (an error counts as unsafe)."""
    try:
        root = Path(cwd).resolve()
        folder = root / FOLDER
        for item in (folder, *(folder / name for name in names)):
            if _is_link(item):
                return False
            item.resolve().relative_to(root)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def replace_file(path: Path, text: str, prefix: str, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``text``: written to a new temporary file next to it (created exclusively, with a
    unique name), then moved over it. ``mode`` is the new file's permission bits; None keeps the old file's, or
    0o644 for a new one. The temporary file is removed when anything fails, and the error is raised."""
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        if mode is None:
            try:
                mode = path.stat().st_mode & 0o777
            except OSError:
                mode = 0o644
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
