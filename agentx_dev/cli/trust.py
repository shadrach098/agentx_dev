"""Which folders the user has said they trust, and the question asked the first time agentx starts in a new one.

agentx can read, edit and run things in the folder it starts in, and a folder you just cloned can contain
instructions (``AGENTX.md``) and settings (``.agentx/settings.json``) written by someone else. So nothing from
the folder is read until the user has said yes to this folder, once. The answer is kept in
``~/.agentx/trusted.json``."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from rich.console import Console
from rich.text import Text

from agentx_dev.cli.ui import CANCELLED, discard_typed_ahead, plain

TRUST_FILE = "trusted.json"
TRUST_OPTIONS = ("Yes, I trust this folder", "No, exit")
TRUST_DEFAULT = 1                     # the menu opens on "No, exit"; Yes is y, 1 or a deliberate pick


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


def _read(home: Path) -> dict:
    """The whole trust file, or {} (missing or damaged). Never raises."""
    try:
        data = json.loads(trust_file(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(home: Path, data: dict) -> bool:
    """Replace the trust file with ``data`` (every key kept). Best effort: False when it could not be saved."""
    try:
        path = trust_file(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.stem}-{os.getpid()}-{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return True
    except (OSError, ValueError):
        return False


def trusted_folders(home: Path) -> List[str]:
    """The folders on file, as comparison keys. Never raises: a missing or damaged file is an empty list."""
    folders = _read(home).get("folders")
    return [os.path.normcase(str(f)) for f in folders if isinstance(f, str)] if isinstance(folders, list) else []


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
    data = _read(home)
    folders = trusted_folders(home)
    key = _key(cwd)
    if key not in folders:
        folders.append(key)
    data["folders"] = folders
    return _write(home, data)


# -- a project's allow rules the user said yes to ---------------------------------------------------------------
# A project's ``.agentx/settings.json`` may list allow rules (actions that then run without a question). They are
# shown and asked about at start; a yes is kept here, in the user's own file, per folder, so the same rules are not
# asked about at every start. A rule the project adds later is asked about again; a no is not kept.

def accepted_project_rules(cwd: Path, home: Path) -> List[str]:
    """The project allow rules the user accepted for ``cwd`` (exactly that folder). Never raises."""
    found = _read(home).get("projectRules")
    rules = found.get(_key(cwd)) if isinstance(found, dict) else None
    return [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []


def remember_project_rules(cwd: Path, home: Path, rules: Sequence[str]) -> bool:
    """Keep ``rules`` as accepted for ``cwd`` (added to the ones accepted before). Best effort."""
    data = _read(home)
    every = data.get("projectRules") if isinstance(data.get("projectRules"), dict) else {}
    key = _key(cwd)
    kept = [r for r in every.get(key, []) if isinstance(r, str)]
    every[key] = kept + [str(r) for r in rules if str(r) not in kept]
    data["projectRules"] = every
    return _write(home, data)


def ensure_trusted(cwd: Path, home: Path, console: Console,
                   select: Optional[Callable[..., Optional[int]]] = None,
                   read_line: Optional[Callable[[str], str]] = None) -> bool:
    """True when the user trusts ``cwd`` (already on file, or just said yes); False when they declined or the
    question could not be answered. Anything but a clear yes is a no: the menu opens on "No, exit"
    (``TRUST_DEFAULT``), so Enter alone declines, and keys typed before the question was shown are thrown away.
    Yes is ``y``, ``1``, or moving to it and pressing Enter."""
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
    discard_typed_ahead()             # an Enter typed while agentx started is not an answer to this
    try:
        if select is not None:        # "No, exit" is highlighted: a stray Enter never trusts a folder
            answer = select(list(TRUST_OPTIONS), TRUST_DEFAULT, {"y": 0, "n": 1})
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
