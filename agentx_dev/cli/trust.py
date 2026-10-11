"""Which folders the user has said they trust, and the question asked the first time agentx starts in a new one.

agentx can read, edit and run things in the folder it starts in, and a folder you just cloned can contain
instructions (``AGENTX.md``) and settings (``.agentx/settings.json``) written by someone else. So nothing from
the folder is read until the user has said yes to this folder, once. The answer is kept in
``~/.agentx/trusted.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from rich.console import Console
from rich.text import Text

from agentx_dev.cli.ui import CANCELLED, plain

TRUST_FILE = "trusted.json"
TRUST_OPTIONS = ("Yes, I trust this folder", "No, exit")


def trust_file(home: Path) -> Path:
    return Path(home) / ".agentx" / TRUST_FILE


def _key(path: Path) -> str:
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(path))
    return os.path.normcase(str(resolved))


def _is_anchor(path: Path, home: Path) -> bool:
    """Home and a filesystem root cover only themselves: trusting them must not trust everything below."""
    key = _key(path)
    return key == _key(home) or Path(key).parent == Path(key)


def trusted_folders(home: Path) -> List[str]:
    """The folders on file, as comparison keys. Never raises: a missing or damaged file is an empty list."""
    try:
        data = json.loads(trust_file(home).read_text(encoding="utf-8"))
        folders = data.get("folders") if isinstance(data, dict) else None
        return [os.path.normcase(str(f)) for f in folders if isinstance(f, str)] if isinstance(folders, list) else []
    except (OSError, ValueError, RecursionError):
        return []


def is_trusted(cwd: Path, home: Path) -> bool:
    """Whether ``cwd`` is a folder the user trusted, or inside one (not below home or a filesystem root)."""
    here = Path(_key(cwd))
    for entry in trusted_folders(home):
        folder = Path(entry)
        if here == folder:
            return True
        if folder in here.parents and not _is_anchor(folder, home):
            return True
    return False


def remember_trust(cwd: Path, home: Path) -> bool:
    """Write ``cwd`` to the trust file. Best effort: returns False when it could not be saved."""
    try:
        folders = trusted_folders(home)
        key = _key(cwd)
        if key not in folders:
            folders.append(key)
        path = trust_file(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"folders": folders}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return True
    except (OSError, ValueError):
        return False


def ensure_trusted(cwd: Path, home: Path, console: Console,
                   select: Optional[Callable[..., Optional[int]]] = None,
                   read_line: Optional[Callable[[str], str]] = None) -> bool:
    """True when the user trusts ``cwd`` (already on file, or just said yes); False when they declined or the
    question could not be answered. Anything but a clear yes is a no."""
    if is_trusted(cwd, home):
        return True
    where = plain(str(cwd))
    broad = _is_anchor(Path(cwd), home)
    console.print(Text("Accessing workspace:", style="bold"))
    console.print(Text(f"\n {where}\n", style="cyan"))
    console.print(Text(
        "Quick safety check: is this a project you created or one you trust? (Your own code, a well-known "
        "open source project, or work from your team.) If not, take a moment to review what is in this folder "
        "first.\n", style=""))
    if broad:
        console.print(Text("This is your home folder or a drive root, which holds a lot more than one project. "
                           "Starting agentx in a project folder is safer.\n", style="bold yellow"))
    console.print(Text("agentx will be able to read files here, and to edit files and run commands once you "
                       "approve them (or without asking, if you choose the accept-edits or auto mode). "
                       "Instructions and settings in this folder will be used.\n"))
    answer: Optional[int] = None
    try:
        if select is not None:
            answer = select(list(TRUST_OPTIONS), 0, {"y": 0, "n": 1})
        if answer is None:
            reader = read_line or input
            raw = str(reader("Trust this folder? [y/N] ")).strip().lower()
            answer = 0 if raw in ("y", "yes") else 1
    except (EOFError, KeyboardInterrupt):
        return False
    except Exception:
        return False
    if answer != 0 or answer == CANCELLED:
        return False
    if not remember_trust(cwd, home):
        console.print(Text("(Could not save this choice; you will be asked again next time.)", style="dim"))
    return True
