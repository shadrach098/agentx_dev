"""Runs a task through the real agentx app on a fresh copy of its sample repo, and measures it."""

from __future__ import annotations

import difflib
import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from rich.console import Console

from bench.tasks import REPOS, Task, Workspace, snapshot


@dataclass
class Result:
    task: str
    passed: bool
    note: str
    seconds: float
    requests: int
    tokens_in: int
    tokens_out: int
    cached: int
    counted: int                   # what the token limit counts: cached input as a tenth
    tool_calls: int                # the chat agent's own calls
    team_tool_uses: int            # every helper's calls, for team jobs
    team_jobs: int
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _diff(before_root: Path, after_root: Path) -> str:
    """A unified diff of every file the run changed (agentx's own folder and caches left out)."""
    before, after = snapshot(before_root), snapshot(after_root)
    out: List[str] = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        old = (before_root / name).read_text(encoding="utf-8", errors="replace").splitlines(True) if name in before else []
        new = (after_root / name).read_text(encoding="utf-8", errors="replace").splitlines(True) if name in after else []
        out.extend(difflib.unified_diff(old, new, f"a/{name}", f"b/{name}"))
    return "".join(out)


def run_task(task: Task, model: str, *, log_dir: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
             model_factory: Optional[Callable[..., Any]] = None, keep: bool = False) -> Result:
    """``task`` on a fresh copy of its repo, in auto mode, with a fresh agentx home (only the built-in skills, no
    MCP servers, no notes), so runs compare. Every question agentx asks is answered no, so the token budget wraps
    the turn up the way a no at the limit question does."""
    from agentx_dev.cli.app import ChatApp
    from agentx_dev.cli.team import counted_tokens

    work = Path(tempfile.mkdtemp(prefix=f"agentx-bench-{task.id}-"))
    repo, home = work / task.repo, work / "home"
    shutil.copytree(REPOS / task.repo, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    home.mkdir()
    before = snapshot(repo)
    log = io.StringIO()
    console = Console(file=log, width=140, force_terminal=False, color_system=None)
    answers = iter([task.prompt, "/exit"])

    def read_line(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError() from None

    extra: Dict[str, Any] = {"model_factory": model_factory} if model_factory is not None else {}
    app = ChatApp(cwd=repo, home=home, console=console, read_line=read_line, read_short=lambda prompt="": "n",
                  model_name=model, mode="auto", env=dict(os.environ if env is None else env),
                  token_limit=task.budget, **extra)
    finals: List[str] = []
    counts = {"tools": 0, "team": 0, "jobs": 0}
    show = app.screen.show_event

    def watch(event: Any) -> None:
        if isinstance(event, dict):
            if event.get("type") == "tool_call":
                counts["tools"] += 1
                counts["jobs"] += event.get("name") == "start_team_job"
            elif event.get("type") == "final":
                finals.append(str(event.get("content") or ""))
        show(event)

    app.screen.show_event = watch
    team_notify = app._team_notify

    def watch_team(kind: str, data: Dict[str, Any]) -> None:
        status = data.get("status")
        if kind == "result" and status is not None:
            counts["team"] += int(status.snapshot().get("tool_uses") or 0)
        team_notify(kind, data)

    app._team_notify = watch_team
    started, error = time.monotonic(), ""
    try:
        app.start()
    except BaseException as exc:                     # noqa: BLE001 - a crash is a failed task, reported
        error = f"{type(exc).__name__}: {exc}"
    seconds = time.monotonic() - started
    requests, tokens_in, tokens_out, cached = app._usage_totals()
    answer = finals[-1] if finals else ""
    if error:
        passed, note = False, f"agentx stopped: {error}"
    else:
        try:
            passed, note = task.check(Workspace(repo, before, answer))
        except Exception as exc:                     # noqa: BLE001
            passed, note = False, f"the check failed: {type(exc).__name__}: {exc}"
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{task.id}.log").write_text(
            f"task: {task.id}\nprompt: {task.prompt}\nresult: {'PASS' if passed else 'FAIL'} - {note}\n\n"
            f"{log.getvalue()}\n\n--- changes ---\n{_diff(REPOS / task.repo, repo)}", encoding="utf-8")
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    return Result(task.id, passed, note, round(seconds, 1), requests, tokens_in, tokens_out, cached,
                  counted_tokens(tokens_in, tokens_out, cached), counts["tools"], counts["team"], counts["jobs"],
                  error)


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=10).stdout.strip()
    except Exception:
        return ""
