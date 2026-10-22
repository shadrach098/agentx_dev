"""Settings for the agentx command: ~/.agentx/settings.json (user) and <project>/.agentx/settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentx_dev.cli.approval import MODES
from agentx_dev.cli.models import parse_team_models, provider_for
from agentx_dev.cli.safepaths import FOLDER, replace_file, safe_location

SETTINGS_NAME = "settings.json"
PLACEHOLDER_MODEL = "(no model)"           # what the status bar shows before a model is built; never a setting
DEFAULT_TOKEN_LIMIT = 200_000              # tokens one message may use before agentx stops and asks; 0 is off


@dataclass
class Settings:
    model: Optional[str] = None
    mode: Optional[str] = None
    add_dirs: List[str] = field(default_factory=list)
    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)     # one line per unreadable file or bad value
    project_allow: List[str] = field(default_factory=list)   # the allow rules that came from the project file
    model_source: Optional[Path] = None                    # the file ``model`` was read from
    team_models: Optional[List[str]] = None                # "teamModels": None when no file sets it
    team_models_source: Optional[Path] = None              # the file the list was read from
    team_model_uses: Dict[str, str] = field(default_factory=dict)   # notes on what a listed model is for
    token_limit: int = DEFAULT_TOKEN_LIMIT                 # "tokenLimit": tokens a message may use; 0 is off


def user_settings_path(home: Path) -> Path:
    return Path(home) / FOLDER / SETTINGS_NAME


def project_settings_path(cwd: Path) -> Path:
    return Path(cwd) / FOLDER / SETTINGS_NAME


def _real_model(model: Any) -> Optional[str]:
    """``model`` when it is a name a provider runs (``provider_for``), else None: neither the ``(no model)``
    placeholder nor a class name used as a label is ever written to a settings file."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip()
    return name if provider_for(name) is not None else None


def ensure_user_settings(home: Path, model: Optional[str] = None) -> Optional[Path]:
    """Create ``~/.agentx/settings.json`` with the current choices written out, so there is a file to edit.

    Does nothing when the file already exists (it is never overwritten) and never raises: the path of the file
    it created is returned, or None. ``model`` is the model in use now, written only when it is a real model
    name (see ``_real_model``); ``permissions`` starts empty."""
    path = user_settings_path(home)
    try:
        if path.exists():
            return None
        data: dict = {}
        name = _real_model(model)
        if name:
            data["model"] = name
        data.update({"mode": "default", "addDirs": [], "permissions": {"allow": [], "deny": []}})
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:        # "x": never replace a file made in between
            handle.write(json.dumps(data, indent=2) + "\n")
        return path
    except (OSError, ValueError, TypeError):
        return None


def is_project_folder(cwd: Path, home: Path) -> bool:
    """False for the user's home folder and a filesystem root: neither is a project, so no folder settings
    file and no work log are kept there. Never raises."""
    try:
        here = Path(cwd).resolve()
        return here != Path(home).resolve() and here != Path(here.anchor)
    except (OSError, RuntimeError, ValueError):
        return False


def _write_project_key(cwd: Path, key: str, value: Any, what: str) -> Tuple[Optional[Path], str]:
    """Set ``key`` to ``value`` in ``<cwd>/.agentx/settings.json``, creating the file or keeping its other keys.
    Returns ``(path, message)``; ``path`` is None when nothing was written (``.agentx`` or the file is a link or
    leads outside the folder, the file is not valid JSON or not an object, or it could not be written), and the
    message says why. ``what`` names the change in the message. Never raises."""
    return _write_key(Path(cwd), key, value, what)


def write_user_token_limit(home: Path, limit: int) -> Tuple[Optional[Path], str]:
    """Save ``limit`` as ``tokenLimit`` in ``~/.agentx/settings.json`` (``/limit``). See ``_write_project_key``."""
    what = f"tokenLimit {limit}" if limit else "tokenLimit 0 (no limit)"
    return _write_key(Path(home), "tokenLimit", int(limit), what)


def _write_key(folder: Path, key: str, value: Any, what: str) -> Tuple[Optional[Path], str]:
    path = folder / FOLDER / SETTINGS_NAME
    if not safe_location(folder, SETTINGS_NAME):
        return None, f"{path} is a link or points outside the folder, so it was left unchanged."
    try:
        data: dict = {}
        verb = "Created"
        if path.exists():
            verb = "Updated"
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return None, f"{path} is not valid JSON, so it was left unchanged."
            if not isinstance(loaded, dict):
                return None, f"{path} is not a JSON object, so it was left unchanged."
            data = loaded
        data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, json.dumps(data, indent=2) + "\n", prefix=".settings-")
        return path, f"{verb} {path} with {what}."
    except (OSError, ValueError, TypeError):
        return None, f"Could not write {path}."


def write_project_model(cwd: Path, model: str) -> Tuple[Optional[Path], str]:
    """Save ``model`` as this folder's model (``/model <name> --folder``). See ``_write_project_key``."""
    return _write_project_key(cwd, "model", str(model), f"model {model}")


def write_project_team_models(cwd: Path, names: Sequence[str],
                              uses: Optional[Mapping[str, str]] = None) -> Tuple[Optional[Path], str]:
    """Save ``names`` as this folder's ``teamModels`` (``/team-models ... --folder``); a name with a note in
    ``uses`` is saved as ``{"model": name, "use": note}``. See ``_write_project_key``."""
    listed = [str(name) for name in names]
    entries = [{"model": name, "use": str(uses[name])} if uses and uses.get(name) else name for name in listed]
    what = ("helper models " + ", ".join(listed)) if listed else "no helper models"
    return _write_project_key(cwd, "teamModels", entries, what)


def ensure_project_settings(cwd: Path, home: Path, model: Optional[str]) -> Optional[Path]:
    """For a folder the user has just trusted: write ``{"model": model}`` to its settings file, unless one
    exists. Nothing is written for the home folder, a drive root, a ``model`` that is not a real model name
    (``_real_model``), or when ``.agentx`` or the file is a link or leads outside the folder. Never overwrites,
    never raises; returns the path it created, or None."""
    name = _real_model(model)
    if not name or not is_project_folder(cwd, home) or not safe_location(cwd, SETTINGS_NAME):
        return None
    path = project_settings_path(cwd)
    try:
        if path.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:        # "x": never replace a file made in between
            handle.write(json.dumps({"model": name}, indent=2) + "\n")
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


def _read_token_limit(value: Any, path: Path, is_project: bool, settings: Settings) -> None:
    """``"tokenLimit"``: a whole number of tokens (0 turns the limit off). A project's file may only lower it: a
    cloned repository must not let a message spend more of the user's money."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        settings.problems.append(f"settings file {path}: 'tokenLimit' must be a whole number of tokens (0 for no "
                                 "limit); ignored")
        return
    if is_project and (value == 0 or (settings.token_limit and value > settings.token_limit)):
        settings.problems.append(f"project settings asked for a higher token limit ({value:,}); ignored (set it in "
                                 "your own settings or with /limit)")
        return
    settings.token_limit = value


def load_settings(cwd: Path, home: Path) -> Settings:
    """User settings first, then the project's: scalars from the project win, lists are concatenated."""
    settings = Settings()
    sources = ((user_settings_path(home), Path(home), False), (project_settings_path(cwd), Path(cwd), True))
    for path, base, is_project in sources:
        data = _read(path, settings.problems)
        model = data.get("model")
        if isinstance(model, str) and model.strip() == PLACEHOLDER_MODEL:
            # an earlier build wrote the status-bar placeholder here; the file is never rewritten for it
            settings.problems.append(f'"model": "{PLACEHOLDER_MODEL}" in {path} is a placeholder written by an '
                                     "earlier build; it was ignored - delete that line.")
        elif isinstance(model, str) and model.strip():
            settings.model = model.strip()
            settings.model_source = path
        elif model is not None:
            settings.problems.append(f"settings file {path}: 'model' must be text; ignored")
        if "teamModels" in data:
            listed = data.get("teamModels")
            if not isinstance(listed, list):
                settings.problems.append(f"settings file {path}: 'teamModels' must be a list of model names; "
                                         "ignored")
            else:
                names, uses, issues = parse_team_models(listed)
                settings.problems += [f"settings file {path}: {issue}" for issue in issues]
                settings.team_models = names               # the folder's list, read second, replaces the user's
                settings.team_model_uses = uses
                settings.team_models_source = path
        if "tokenLimit" in data:
            _read_token_limit(data.get("tokenLimit"), path, is_project, settings)
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
