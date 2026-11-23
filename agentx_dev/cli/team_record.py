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
                 when: Optional[datetime] = None, conversation: str = "") -> None:
        self.redact = redact
        self.task = str(task or "")
        self.conversation = re.sub(r"[^A-Za-z0-9-]", "", str(conversation or ""))[:80]   # the session that ran it
        self.when = when or datetime.now()
        self.folder: Optional[Path] = None
        self._steps: Dict[int, Path] = {}
        self._plan: List[Dict[str, Any]] = []
        self._result = ""
        try:
            base = Path(folder)
            name = f"{self.when:%Y-%m-%d-%H%M%S}-{_slug(self.task)}"
            base.mkdir(parents=True, exist_ok=True)
            target, number = base / name, 2
            while True:                         # two jobs in one second (two conversations): the next free name
                try:
                    target.mkdir()
                    break
                except FileExistsError:
                    target, number = base / f"{name}-{number}", number + 1
            self.folder = target
            self._write_job()
        except OSError as e:
            logger.warning("Could not create the team job record: %s", e)
            self.folder = None

    @classmethod
    def for_project(cls, cwd: Path, task: str, redact: Callable[[str], str] = str,
                    conversation: str = "") -> Optional["TeamRecord"]:
        """In ``<cwd>/.agentx/team``; None when ``.agentx`` or ``team`` is a link or leads out of the folder."""
        if not safe_location(cwd, TEAM):
            return None
        record = cls(Path(cwd) / FOLDER / TEAM, task, redact, conversation=conversation)
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
        lines = [f"# Team job: {_first_line(self.task, 120)}", f"Date: {self.when:%Y-%m-%d %H:%M}",
                 *([f"Conversation: {self.conversation}"] if self.conversation else []), "",
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
        fallback = getattr(status, "fallback", None)
        if ok and isinstance(fallback, dict) and fallback.get("to"):
            state += f" on {_first_line(fallback['to'], 80)} (fallback)"
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


# -- reading the jobs back (the app window's Details, for a conversation opened again) ------------------------------

_STARTED = re.compile(r"^Started (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.", re.M)
_CONVERSATION = re.compile(r"^Conversation: ([A-Za-z0-9-]+)\s*$", re.M)
_STEP_STATE = re.compile(r"Status: \*\*(\w+)\*\*\.?(?: ([\d,]+) tool uses, ([\d,]+) tokens\.)?")


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n\n(.*?)(?=\n## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else ""


def _same_task(saved: Any, asked: Any) -> bool:
    """A job's saved Task section (which may run on into a note) starts with the task a history asked for."""
    plain = lambda text: " ".join(str(text or "").split()).lower()          # noqa: E731
    start = plain(asked)[:80]
    return bool(start) and plain(saved).startswith(start)


def read_jobs(folder: Path, conversation: str = "", since: Optional[datetime] = None,
              until: Optional[datetime] = None, limit: int = 20,
              tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The team jobs saved in ``folder`` (a project's ``.agentx/team``) that a conversation ran: those whose
    ``job.md`` names ``conversation``; an older one without that line when its task is one of ``tasks`` (the
    jobs the conversation's history asked for) or, when ``tasks`` is empty (a history summarised since), when it
    started between ``since`` and ``until`` (local time). Oldest first, each ``{"title", "started", "steps"}``;
    a step is ``{"helper", "label", "state", "toolUses", "tokens", "query"}``. Nothing here raises."""
    jobs: List[Dict[str, Any]] = []
    try:
        entries = sorted(p for p in Path(folder).iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return jobs
    for entry in entries[-400:]:
        text = _read(entry / "job.md", 40_000)
        if not text:
            continue
        named = _CONVERSATION.search(text)
        started_match = _STARTED.search(text)
        try:
            started = datetime.strptime(started_match.group(1), "%Y-%m-%d %H:%M:%S") if started_match else None
        except ValueError:
            started = None
        if named:
            if not conversation or named.group(1) != conversation:
                continue
        elif tasks:
            if not any(_same_task(_section(text, "Task"), task) for task in tasks):
                continue
        elif not (started and since and until and since <= started <= until):
            continue
        title = re.sub(r"^# Team job: ", "", text.split("\n", 1)[0]).strip()
        steps = []
        for step_file in sorted(entry.glob("[0-9][0-9]-*.md")):
            body = _read(step_file, 60_000)
            head = body.split("\n", 1)[0].lstrip("# ").strip()
            helper, _, label = head.partition(" · ")
            state = _STEP_STATE.search(body)
            steps.append({"helper": helper.strip(), "label": label.strip(),
                          "state": state.group(1) if state else "running",
                          "toolUses": int(state.group(2).replace(",", "")) if state and state.group(2) else 0,
                          "tokens": int(state.group(3).replace(",", "")) if state and state.group(3) else 0,
                          "query": _first_line(_section(body, "Asked"), 600)})
        jobs.append({"title": title, "started": started.isoformat(sep=" ") if started else "",
                     "folder": entry.name, "steps": steps})
    return jobs[-limit:]
