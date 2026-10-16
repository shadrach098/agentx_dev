"""The skills that ship with agentx, how they are created in the user's skills folder, and the generated skill
that describes the agent's own tools.

Rules for the copies in the skills folder:

* A default that is not there and has never been created is written (first run, or a new default in a newer
  agentx). The names created so far are kept in ``.state.json`` so that a default the user deleted stays deleted.
* A copy agentx wrote and the user has not changed is replaced when agentx ships a newer version of it.
* A copy the user edited, and any skill the user made themselves under the same name, is never touched.
* Nothing is written when nothing changed, so file times stay put."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable, List, Optional, Set

from .Store import (
    MAX_BODY_CHARS, SIDECAR, SKILL_FILE, USE_SKILL, Skill, _is_link, one_line, parse_skill_md, read_sidecar,
    write_bytes_atomic,
)

logger = logging.getLogger(__name__)

STATE_FILE = ".state.json"
CUSTOM_TOOLS = "custom-tools"


@dataclass(frozen=True)
class Bundled:
    name: str
    text: str
    version: int


def bundled_skills(root=None) -> List[Bundled]:
    """The skills in ``agentx_dev/resources/skills`` (or in ``root``, a folder, for tests), sorted by name."""
    try:
        base = root if root is not None else resources.files("agentx_dev.resources").joinpath("skills")
        entries = sorted((e for e in base.iterdir() if e.is_dir()), key=lambda e: e.name)
    except (OSError, ModuleNotFoundError):
        return []
    found: List[Bundled] = []
    for entry in entries:
        text = entry.joinpath(SKILL_FILE).read_text(encoding="utf-8")
        meta, _ = parse_skill_md(text)
        found.append(Bundled(entry.name, text, int(meta.get("version", 1))))
    return found


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> Optional[str]:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return None


def _read_seen(target: Path) -> Set[str]:
    try:
        data = json.loads((target / STATE_FILE).read_text(encoding="utf-8"))
        return {n for n in data.get("seen", []) if isinstance(n, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def _write_managed(folder: Path, skill: Bundled) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = skill.text.encode("utf-8")
    write_bytes_atomic(folder / SKILL_FILE, data)
    sidecar = {"managed": True, "version": skill.version, "sha256": _sha(data)}
    write_bytes_atomic(folder / SIDECAR, json.dumps(sidecar, indent=2).encode("utf-8"))


def seed_defaults(target: Path, bundled: Optional[Iterable[Bundled]] = None) -> List[str]:
    """Create or update the bundled skills in ``target`` by the rules in the module docstring. Returns the names
    written. Never raises: a folder that cannot be written is logged and the agent runs without the copies."""
    target = Path(target)
    written: List[str] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        seen = _read_seen(target)
        before = set(seen)
        for skill in (bundled if bundled is not None else bundled_skills()):
            folder = target / skill.name
            try:
                if not os.path.lexists(folder):
                    if skill.name in seen:
                        continue                                  # the user deleted it: it stays deleted
                    _write_managed(folder, skill)
                    written.append(skill.name)
                elif not _is_link(folder):
                    sidecar = read_sidecar(folder)
                    if sidecar.get("managed") is True and int(sidecar.get("version") or 0) < skill.version:
                        current = _file_sha(folder / SKILL_FILE)
                        if current is not None and current == sidecar.get("sha256"):    # not edited by the user
                            _write_managed(folder, skill)
                            written.append(skill.name)
                seen.add(skill.name)
            except (OSError, ValueError) as e:
                logger.warning("Could not create the default skill %s in %s: %s", skill.name, target, e)
        if seen != before:
            write_bytes_atomic(target / STATE_FILE, json.dumps({"seen": sorted(seen)}).encode("utf-8"))
    except OSError as e:
        logger.warning("Could not set up the default skills in %s: %s", target, e)
    return written


def _argument_names(tool: object) -> str:
    fields = getattr(getattr(tool, "args_schema", None), "model_fields", None)
    return ", ".join(fields) if fields else "one text input"


def custom_tools_skill(tools: Iterable[object], covered: Set[str]) -> Optional[Skill]:
    """A skill generated in memory (never written to disk) that maps the agent's tools no other skill covers: its
    own tools. None when every tool is covered. The same tools always give the same text."""
    rest = sorted((t for t in tools if t.name not in covered and t.name != USE_SKILL), key=lambda t: t.name)
    if not rest:
        return None
    sections = [f"## {t.name}\n{one_line(t.description, 600)}\nArguments: {_argument_names(t)}" for t in rest]
    body = ("Tools added to this agent beyond the built-in ones. Each tool's own description is the full "
            "contract; this is a quick map.\n\n" + "\n\n".join(sections))
    return Skill(name=CUSTOM_TOOLS, description="What the agent's own tools do: " + ", ".join(t.name for t in rest),
                 body=body[:MAX_BODY_CHARS], tools=tuple(t.name for t in rest), source="generated")
