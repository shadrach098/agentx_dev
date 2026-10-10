"""Settings for the agentx command: ~/.agentx/settings.json (user) and <project>/.agentx/settings.json."""

from __future__ import annotations

import json
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


def user_settings_path(home: Path) -> Path:
    return Path(home) / ".agentx" / "settings.json"


def project_settings_path(cwd: Path) -> Path:
    return Path(cwd) / ".agentx" / "settings.json"


def _read(path: Path, problems: List[str]) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        problems.append(f"settings file {path}: {e}; ignored")
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


def load_settings(cwd: Path, home: Path) -> Settings:
    """User settings first, then the project's: scalars from the project win, lists are concatenated."""
    settings = Settings()
    for path in (user_settings_path(home), project_settings_path(cwd)):
        data = _read(path, settings.problems)
        model = data.get("model")
        if isinstance(model, str) and model.strip():
            settings.model = model.strip()
        elif model is not None:
            settings.problems.append(f"settings file {path}: 'model' must be text; ignored")
        mode = data.get("mode")
        if mode is not None:
            if isinstance(mode, str) and mode in MODES:
                settings.mode = mode
            else:
                settings.problems.append(
                    f"settings file {path}: 'mode' must be one of {', '.join(MODES)}; ignored")
        settings.add_dirs += _strings(data.get("addDirs"), "addDirs", path, settings.problems)
        permissions = data.get("permissions")
        if permissions is not None and not isinstance(permissions, dict):
            settings.problems.append(f"settings file {path}: 'permissions' must be an object; ignored")
            permissions = None
        permissions = permissions or {}
        settings.allow += _strings(permissions.get("allow"), "permissions.allow", path, settings.problems)
        settings.deny += _strings(permissions.get("deny"), "permissions.deny", path, settings.problems)
    return settings
