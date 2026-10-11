"""Settings for the agentx command: ~/.agentx/settings.json (user) and <project>/.agentx/settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from agentx_dev.cli.approval import MODES


@dataclass
class Settings:
    model: Optional[str] = None
    mode: Optional[str] = None
    add_dirs: List[str] = field(default_factory=list)
    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)     # one line per unreadable file or bad value
    project_allow: List[str] = field(default_factory=list)   # the allow rules that came from the project file


def user_settings_path(home: Path) -> Path:
    return Path(home) / ".agentx" / "settings.json"


def project_settings_path(cwd: Path) -> Path:
    return Path(cwd) / ".agentx" / "settings.json"


def ensure_user_settings(home: Path, model: Optional[str] = None) -> Optional[Path]:
    """Create ``~/.agentx/settings.json`` with the current choices written out, so there is a file to edit.

    Does nothing when the file already exists (it is never overwritten) and never raises: the path of the file
    it created is returned, or None. ``model`` is the model in use now; ``permissions`` starts empty."""
    path = user_settings_path(home)
    try:
        if path.exists():
            return None
        data: dict = {}
        if model:
            data["model"] = str(model)
        data.update({"mode": "default", "addDirs": [], "permissions": {"allow": [], "deny": []}})
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:        # "x": never replace a file made in between
            handle.write(json.dumps(data, indent=2) + "\n")
        return path
    except (OSError, ValueError, TypeError):
        return None


def _clean(text: object, limit: int = 200) -> str:
    """Error text made safe to print: control characters become '?', and it is cut at ``limit``."""
    text = "".join(c if c.isprintable() else "?" for c in str(text))
    return text if len(text) <= limit else text[:limit] + "..."


def _read(path: Path, problems: List[str]) -> dict:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        problems.append(f"settings file {path}: {_clean(e)}; ignored")
        return {}
    if not isinstance(data, dict):
        problems.append(f"settings file {path}: expected a JSON object; ignored")
        return {}
    return data


def _strings(value: Any, where: str, path: Path, problems: List[str]) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"settings file {path}: '{where}' must be a list of text; ignored")
        return []
    kept = [v for v in value if isinstance(v, str) and v.strip()]
    if len(kept) != len(value):
        problems.append(f"settings file {path}: '{where}' has entries that are not text; skipped")
    return kept


def _resolve(target: Path) -> Path:
    try:
        return target.resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(target))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _absolute(entry: str, base: Path, home: Path) -> Path:
    """An addDirs entry as an absolute path: '~' means ``home``, relative entries start at ``base``."""
    text = entry.strip()
    if text == "~" or text.startswith(("~/", "~\\")):
        target = Path(home) / text[2:] if len(text) > 1 else Path(home)
    else:
        target = Path(text)
        if not target.is_absolute():
            target = Path(base) / target
    return _resolve(target)


def load_settings(cwd: Path, home: Path) -> Settings:
    """User settings first, then the project's: scalars from the project win, lists are concatenated."""
    settings = Settings()
    sources = ((user_settings_path(home), Path(home), False), (project_settings_path(cwd), Path(cwd), True))
    for path, base, is_project in sources:
        data = _read(path, settings.problems)
        model = data.get("model")
        if isinstance(model, str) and model.strip():
            settings.model = model.strip()
        elif model is not None:
            settings.problems.append(f"settings file {path}: 'model' must be text; ignored")
        mode = data.get("mode")
        if mode is not None:
            if is_project and mode in ("accept-edits", "auto"):
                # a cloned repository must not switch off the prompts on its own
                settings.problems.append(f"project settings asked for {_clean(mode, 40)} mode; ignored "
                                         "(set it in your own settings or with --mode)")
            elif isinstance(mode, str) and mode in MODES:
                settings.mode = mode
            else:
                settings.problems.append(
                    f"settings file {path}: 'mode' must be one of {', '.join(MODES)}; ignored")
        dirs = _strings(data.get("addDirs"), "addDirs", path, settings.problems)
        for entry in dirs:
            resolved = _absolute(entry, base, home)
            if is_project and not _inside(resolved, _resolve(Path(cwd))):
                # a cloned repository must not widen the tool sandbox on its own
                settings.problems.append(
                    f"project settings tried to add {str(resolved)!r} outside the project; ignored "
                    "(add it in your own settings or with --add-dir)")
                continue
            settings.add_dirs.append(str(resolved))
        permissions = data.get("permissions")
        if permissions is not None and not isinstance(permissions, dict):
            settings.problems.append(f"settings file {path}: 'permissions' must be an object; ignored")
            permissions = None
        permissions = permissions or {}
        allowed = _strings(permissions.get("allow"), "permissions.allow", path, settings.problems)
        settings.allow += allowed
        if is_project:
            settings.project_allow += allowed
        settings.deny += _strings(permissions.get("deny"), "permissions.deny", path, settings.problems)
    return settings
