"""Which skills the user approved, file by file, so the agent cannot add or change one behind their back.

A skill is instructions the agent follows. ``/skills install`` and ``/skills trust`` (the user's own commands; no
tool reaches them) inspect a skill and, after the user's yes, keep the digest of its exact files in
``~/.agentx/skills-trust.json``. A skill is offered to the agent only while its files still have that digest; one
that appeared or changed any other way (copied in by a shell command, edited, unpacked from a cloned repository)
is held back until the user looks at it with ``/skills trust <name>``. The skills agentx ships are recognised by
their text instead.

This makes a change visible, not impossible: shell commands run as the user's own account and could rewrite this
file too. The default deny rules refuse shell and Python commands that name the skills folders or this file, which
stops the plain ways of doing it; the digest check catches the rest the next time skills are loaded.

The first time it runs (no record file yet), the skills already in the user's own folder are recorded as they are:
the user installed or wrote them before agentx kept records. A project's skills are never recorded that way."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agentx_dev.Skills.Defaults import bundled_skills
from agentx_dev.Skills.Inspect import Snapshot, SnapshotError, snapshot
from agentx_dev.Skills.Store import SIDECAR, SKILL_FILE, Skill, SkillStore, one_line, read_sidecar, write_bytes_atomic

TRUST_FILE = "skills-trust.json"

NOT_APPROVED = "it was not installed or approved with /skills; /skills trust {name} inspects it"
CHANGED = "its files changed since you approved it; /skills trust {name} inspects it again"
UNREADABLE = "it cannot be read as one plain folder ({why})"


def _key(folder: Path) -> str:
    try:
        resolved = Path(folder).resolve()
    except (OSError, RuntimeError):
        resolved = Path(folder).absolute()
    return os.path.normcase(str(resolved))


def _signature(folder: Path) -> Optional[Tuple]:
    """Every file's path, size and change time: when it is the same as last time, so is the digest."""
    found = []
    try:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                st = os.lstat(os.path.join(dirpath, name))
                found.append((os.path.relpath(os.path.join(dirpath, name), folder), st.st_size, st.st_mtime_ns,
                              st.st_mode))
            for name in dirnames:
                st = os.lstat(os.path.join(dirpath, name))
                found.append((os.path.relpath(os.path.join(dirpath, name), folder), -1, st.st_mtime_ns, st.st_mode))
    except OSError:
        return None
    return tuple(found)


class SkillTrust:
    """The record of approved skills for one user (``home``)."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / ".agentx" / TRUST_FILE
        self.records: Dict[str, Dict[str, Any]] = {}
        self.existed = False
        self._digests: Dict[str, Tuple[Tuple, str]] = {}     # folder -> (signature, digest) seen this session
        self._shipped = {hashlib.sha256(b.text.encode("utf-8")).hexdigest() for b in bundled_skills()}
        self.load()

    # -- the file ----------------------------------------------------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.existed = True
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError, RecursionError):
            data, self.existed = {}, True               # damaged: nothing counts as approved, and nothing is adopted
        skills = data.get("skills") if isinstance(data, dict) else None
        self.records = {k: v for k, v in (skills or {}).items() if isinstance(k, str) and isinstance(v, dict)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "skills": self.records}, indent=2, sort_keys=True)
        write_bytes_atomic(self.path, body.encode("utf-8"))
        self.existed = True

    # -- approving -------------------------------------------------------------------------------------------

    def record(self, folder: Path, snap: Snapshot, *, scope: str, how: str, origin: str = "",
               review: str = "") -> None:
        """Approve ``folder`` as exactly ``snap`` (which includes its sidecar)."""
        self.records[_key(folder)] = {
            "name": Path(folder).name, "scope": scope, "digest": snap.digest, "files": len(snap.files),
            "how": how, "origin": one_line(origin, 300), "review": one_line(review, 300),
            "approved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._digests.pop(_key(folder), None)
        self.save()

    def forget(self, folder: Path) -> None:
        if self.records.pop(_key(folder), None) is not None:
            self.save()

    def adopt_existing(self, user_dir: Optional[Path]) -> List[str]:
        """First run only (no record file yet): record the skills already in the user's own folder as they are.
        Returns their names. Afterwards the file exists (even with no skills), so this never happens again."""
        if self.existed:
            return []
        adopted: List[str] = []
        if user_dir is not None:
            for skill in SkillStore([(Path(user_dir), "user")]).all():
                if skill.folder is None or self.is_shipped(skill):
                    continue
                try:
                    snap = snapshot(skill.folder)
                except SnapshotError:
                    continue
                self.records[_key(skill.folder)] = {
                    "name": skill.folder.name, "scope": "global", "digest": snap.digest, "files": len(snap.files),
                    "how": "already installed when agentx started keeping records", "origin": "", "review": "",
                    "approved": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                adopted.append(skill.name)
        try:
            self.save()
        except OSError:
            pass
        return adopted

    # -- checking --------------------------------------------------------------------------------------------

    def is_shipped(self, skill: Skill) -> bool:
        """A copy of a skill agentx ships, as agentx wrote it: only SKILL.md (one of the shipped texts) and its
        sidecar saying so."""
        folder = skill.folder
        if folder is None or read_sidecar(folder).get("managed") is not True:
            return False
        try:
            snap = snapshot(folder)
        except SnapshotError:
            return False
        if set(snap.files) != {SKILL_FILE, SIDECAR}:
            return False
        return hashlib.sha256(snap.files[SKILL_FILE]).hexdigest() in self._shipped

    def digest(self, folder: Path) -> str:
        """The digest of ``folder`` now; reused while no file's size or time changed. Raises ``SnapshotError``."""
        key = _key(folder)
        signature = _signature(folder)
        cached = self._digests.get(key)
        if signature is not None and cached is not None and cached[0] == signature:
            return cached[1]
        value = snapshot(folder).digest
        if signature is not None:
            self._digests[key] = (signature, value)
        return value

    def check(self, skill: Skill) -> Optional[str]:
        """Why ``skill`` may not be offered to the agent (None when it may). For ``Skills(check=...)``."""
        if skill.folder is None or self.is_shipped(skill):
            return None
        entry = self.records.get(_key(skill.folder))
        if entry is None:
            return NOT_APPROVED.format(name=skill.name)
        try:
            now = self.digest(skill.folder)
        except SnapshotError as e:
            return UNREADABLE.format(why=e)
        if now != entry.get("digest"):
            return CHANGED.format(name=skill.name)
        return None
