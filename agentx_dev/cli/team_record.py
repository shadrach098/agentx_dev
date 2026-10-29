"""A team job written to disk as it runs, so a helper's research survives the terminal closing.

``<project>/.agentx/team/<date>-<time>-<task>/``:

- ``job.md``: the task, the plan when it is made, and the team's result when the job ends;
- ``NN-<helper>.md``, one per step: written when the step starts (what it was asked), then rewritten with the
  helper's whole report (not the one-line summary the screen shows), the files it touched and its tool calls.

A step the job never finished keeps its "running" file, so what was asked is still there. Every write replaces a
file whole (never half a file), text passes through ``redact`` (secrets out) first, and nothing here raises: a
record that cannot be written never stops the job.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agentx_dev.cli.safepaths import FOLDER, replace_file, safe_location

logger = logging.getLogger(__name__)

TEAM = "team"
MAX_TITLE_WORDS = 6


def _slug(text: str, words: int = MAX_TITLE_WORDS) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", " ".join(str(text or "").lower().split()[:words])).strip("-")
    return cleaned[:60].strip("-") or "job"


class TeamRecord:
    """One team job's folder. ``for_project`` puts it in ``<cwd>/.agentx/team``; ``in_folder`` anywhere."""

    def __init__(self, folder: Path, task: str, redact: Callable[[str], str] = str,
                 when: Optional[datetime] = None) -> None:
        self.redact = redact
        self.task = str(task or "")
        self.when = when or datetime.now()
        self.folder: Optional[Path] = None
        self._steps: Dict[int, Path] = {}
        self._plan: List[Dict[str, Any]] = []
        self._result = ""
        try:
            base = Path(folder)
            name = f"{self.when:%Y-%m-%d-%H%M%S}-{_slug(self.task)}"
            target, number = base / name, 2
            while target.exists():
                target, number = base / f"{name}-{number}", number + 1
            target.mkdir(parents=True)
            self.folder = target
            self._write_job()
        except OSError as e:
            logger.warning("Could not create the team job record: %s", e)
            self.folder = None

    @classmethod
    def for_project(cls, cwd: Path, task: str, redact: Callable[[str], str] = str) -> Optional["TeamRecord"]:
        """In ``<cwd>/.agentx/team``; None when ``.agentx`` or ``team`` is a link or leads out of the folder."""
        if not safe_location(cwd, TEAM):
            return None
        record = cls(Path(cwd) / FOLDER / TEAM, task, redact)
        return record if record.folder is not None else None

    @property
    def shown_path(self) -> str:
        """The folder as the agent and the user see it: relative to the project when it is inside one."""
        if self.folder is None:
            return ""
        parts = self.folder.parts
        if FOLDER in parts:
            return "/".join(parts[parts.index(FOLDER):])
        return str(self.folder)

    # -- writing ------------------------------------------------------------------------------------------------

    def _put(self, path: Path, text: str) -> None:
        try:
            replace_file(path, self.redact(text), prefix=".team-")
        except Exception as e:                       # a record must never stop the job
            logger.warning("Could not write %s: %s", path, e)

    def _write_job(self) -> None:
        if self.folder is None:
            return
        lines = [f"# Team job: {_first_line(self.task, 120)}", f"Date: {self.when:%Y-%m-%d %H:%M}", "",
                 f"Started {self.when:%Y-%m-%d %H:%M:%S}.", "",
                 "## Task", "", self.task.strip(), ""]
        if self._plan:
            lines += ["## Plan", ""]
            for number, step in enumerate(self._plan, 1):
                lines.append(f"{number}. **{step.get('agent', '')}** · {step.get('label', '')}: "
                             f"{_first_line(step.get('query', ''), 300)}")
            lines.append("")
        if self._result:
            lines += ["## Result", "", self._result.strip(), ""]
        else:
            lines += ["(The job has not finished. Each step's file says what that step was asked and, once it "
                      "finished, its whole report.)", ""]
        self._put(self.folder / "job.md", "\n".join(lines))

    def plan(self, steps: List[Dict[str, Any]]) -> None:
        self._plan = list(steps or [])
        self._write_job()

    def _step_path(self, slot: int, helper: str) -> Optional[Path]:
        if self.folder is None:
            return None
        if slot not in self._steps:
            name = re.sub(r"[^A-Za-z0-9.@-]+", "-", str(helper or "helper")).strip("-") or "helper"
            self._steps[slot] = self.folder / f"{slot:02d}-{name[:60]}.md"
        return self._steps[slot]

    def started(self, status: Any) -> None:
        path = self._step_path(getattr(status, "slot", len(self._steps) + 1), getattr(status, "helper", ""))
        if path is None:
            return
        self._put(path, "\n".join([
            f"# {getattr(status, 'helper', '')} · {getattr(status, 'label', '')}", "",
            f"Started {datetime.now():%Y-%m-%d %H:%M:%S}. Status: **running** (if this is still here, the job "
            "stopped before the step finished).", "", "## Asked", "", str(getattr(status, "query", "")).strip(), ""]))

    def finished(self, status: Any, result: Any, ok: bool) -> None:
        if status is None:
            return
        path = self._step_path(getattr(status, "slot", len(self._steps) + 1), getattr(status, "helper", ""))
        if path is None:
            return
        report = str(getattr(result, "content", "") or "").strip()
        error = str(getattr(result, "error", "") or "").strip()
        state = "done" if ok else ("skipped" if getattr(result, "skipped", False) else "failed")
        files = list(getattr(status, "files", {}) or {})
        calls = getattr(status, "calls", {}) or {}
        lines = [f"# {getattr(status, 'helper', '')} · {getattr(status, 'label', '')}",
                 f"Date: {datetime.now():%Y-%m-%d %H:%M}", "",
                 f"Finished {datetime.now():%Y-%m-%d %H:%M:%S}. Status: **{state}**. "
                 f"{getattr(status, 'tool_uses', 0)} tool uses, {getattr(status, 'tokens', 0):,} tokens.", "",
                 "## Asked", "", str(getattr(status, "query", "")).strip(), ""]
        if error:
            lines += ["## Error", "", error, ""]
        lines += ["## Report", "", report or "(no report)", ""]
        if files:
            lines += ["## Files", ""] + [f"- {name}" for name in files] + [""]
        if calls:
            lines += ["## Tool calls", ""] + [f"- {name}: {count}" for name, count in calls.items()] + [""]
        self._put(path, "\n".join(lines))

    def done(self, result_text: str) -> None:
        self._result = str(result_text or "")
        self._write_job()


def _first_line(text: Any, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
