"""The background-task tools the main agent gets, each behind the gate, and the text ``/tasks`` shows.

``start_background`` and ``send_to_terminal`` are judged as ``run_shell`` (see ``approval.RULE_AS``); reading, listing
and stopping a task this conversation started are reads. Stopping another conversation's task, or a terminal the user
opened, is asked about first (``stop_other_task``, a command). The agent cannot open a terminal.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from pydantic import BaseModel, Field

from agentx_dev.DefaultTools import Permissions, _resolve_for_ops
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.background import Task, TaskLimit, TaskManager
from agentx_dev.cli.tools import GATE_FAILED, OUTPUT_MAX_CHARS, approval_lock, gate_tool

FIRST_OUTPUT_WAIT_SEC = 2.0     # start_background waits this long at most for the first output (or the end)
SETTLE_SEC = 0.5                # ... and, once output is there, this much longer for the task to end at once
SEND_WAIT_SEC = 1.0             # send_to_terminal reads what followed after this long
START_LINES = 40
DEFAULT_LINES = 80
MAX_LINES = 400
SHOW_LINES = 40                 # /tasks show
COMMAND_SHOWN = 200             # a command in a listing is cut here
TASKS_USAGE = "Usage: /tasks, /tasks show <id>, /tasks stop <id|all>"


def duration(seconds: float) -> str:
    """``12s``, ``2m 10s``, ``1h 5m``."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _age(task: Task) -> float:
    return (task.ended_at or time.time()) - task.started_at


def _one_line(text: str, limit: int = COMMAND_SHOWN) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _clip(text: str) -> str:
    """The newest ``OUTPUT_MAX_CHARS`` of ``text``."""
    if len(text) <= OUTPUT_MAX_CHARS:
        return text
    return "[... older output cut ...]\n" + text[-OUTPUT_MAX_CHARS:]


def unknown(id: str) -> str:
    return f"ERROR: there is no task {id}; list_background shows them"


def header(task: Task) -> str:
    """``t1 (running for 2m 10s): npm run dev``, ``t1 (exited with code 0 after 12s): ...``."""
    if task.state == "running":
        phrase = f"running for {duration(_age(task))}"
    elif task.state == "exited":
        phrase = f"exited with code {task.exit_code} after {duration(_age(task))}"
    elif task.state == "stopped":
        phrase = f"stopped after {duration(_age(task))}"
    else:
        phrase = "could not start"
    if task.kind == "terminal":
        phrase = "terminal, " + phrase
    return f"{task.id} ({phrase}): {task.command}"


def output_text(manager: TaskManager, task: Task, lines: int = DEFAULT_LINES) -> str:
    text = manager.output(task.id, lines)
    return header(task) + "\n" + (_clip(text).rstrip("\n") or "(no output yet)")


def ended_text(task: Task) -> str:
    if task.state == "exited":
        return f"{task.id} had already ended (exit code {task.exit_code})."
    if task.state == "failed":
        return f"{task.id} had already ended (it could not start)."
    return f"{task.id} had already ended (stopped)."


def task_line(task: Task, me: str, titles: Callable[[str], str]) -> str:
    """``t1  running  2m 10s  npm run dev  (started by: <title>)``."""
    state = f"exited (code {task.exit_code})" if task.state == "exited" else task.state
    what = _one_line(task.command)
    if task.name:
        what += f" [{_one_line(task.name, 60)}]"
    if task.kind == "terminal":
        return f"{task.id}  {state}  {duration(_age(task))}  terminal: {what}  (opened by the user)"
    if me and task.owner == me:
        by = "this conversation"
    else:
        try:
            by = titles(task.owner) if task.owner else ""
        except Exception:
            by = ""
        by = _one_line(by, 80) if by else "another conversation"
    return f"{task.id}  {state}  {duration(_age(task))}  {what}  (started by: {by})"


def list_text(manager: TaskManager, me: str, titles: Callable[[str], str]) -> str:
    tasks = manager.list()
    if not tasks:
        return "No background tasks."
    return "\n".join(task_line(task, me, titles) for task in tasks)


def stop_text(manager: TaskManager, task: Task) -> str:
    """Stop ``task`` (no question asked: the caller has decided) and say what happened."""
    if task.state != "running":
        return ended_text(task)
    stopped = manager.stop(task.id)
    if stopped.state == "stopped":
        return f"Stopped {task.id} ({task.command})."
    return ended_text(stopped)                       # it ended on its own just before


def tasks_command(manager: TaskManager, arg: str, me: str, titles: Callable[[str], str]) -> Tuple[str, bool]:
    """``/tasks [show <id>|stop <id|all>]``: (the text to show, whether it worked). The user's own command: nothing
    is asked."""
    words = arg.split()
    if not words:
        return list_text(manager, me, titles), True
    sub = words[0].lower()
    if sub == "stop" and len(words) == 2 and words[1].lower() == "all":
        running = [task.id for task in manager.list() if task.state == "running"]
        if not running:
            return "Nothing is running.", True
        manager.stop_all()
        return f"Stopped {', '.join(running)}.", True
    if sub in ("show", "stop") and len(words) == 2:
        task = manager.get(words[1])
        if task is None:
            return f"There is no task {words[1]}; /tasks lists them.", False
        if sub == "show":
            return output_text(manager, task, SHOW_LINES), True
        return stop_text(manager, task), True
    return TASKS_USAGE, False


# The specs are short: a local model's lean request carries them all (``LEAN_TOOLS``), and the system prompt says
# when to use them.
_ID = "Task or terminal id: t1, term1, ..."


class _StartArgs(BaseModel):
    command: str = Field(..., description="Shell command, run as run_shell runs it.")
    cwd: Optional[str] = Field(None, description="Folder inside the project (default: the project).")
    name: str = Field("", description="Optional label.")


class _OutputArgs(BaseModel):
    id: str = Field(..., description=_ID)
    lines: int = Field(DEFAULT_LINES, description=f"Newest lines to return (at most {MAX_LINES}).")


class _IdArgs(BaseModel):
    id: str = Field(..., description=_ID)


class _NoArgs(BaseModel):
    pass


class _SendArgs(BaseModel):
    id: str = Field(..., description="Terminal id: term1, ...")
    text: str = Field(..., description="The line to type.")


START_DESCRIPTION = ("Start a command that keeps running (a dev server, a watcher) in the background; returns at once "
                     "with its id and first output. Approved like run_shell.")
OUTPUT_DESCRIPTION = "State, exit code and newest lines of a background task or terminal."
STOP_DESCRIPTION = "Stop a background task. Stopping another conversation's task or a user's terminal asks first."
LIST_DESCRIPTION = "List the background tasks and terminals: id, state, age, command, who started it."
SEND_DESCRIPTION = ("Type a line into a terminal the user opened; returns the output after about a second. Approved "
                    "like run_shell.")


def build_task_tools(manager: TaskManager, owner: Callable[[], str], permissions: Permissions, gate: Gate,
                     titles: Callable[[str], str]) -> List[StructuredTool]:
    """``start_background``, ``background_output``, ``stop_background``, ``list_background`` and
    ``send_to_terminal`` over ``manager``, each behind ``gate``. ``owner()`` is the calling conversation's id now (it
    changes with /clear and /resume); ``titles(owner)`` a conversation's title, "" when unknown."""

    def me() -> str:
        try:
            return str(owner() or "")
        except Exception:
            return ""

    def start_background(command: str, cwd: Optional[str] = None, name: str = "") -> str:
        where = None
        if cwd:
            try:
                where = _resolve_for_ops(cwd, permissions)          # inside the sandbox, as run_shell does
            except Exception as error:
                return f"ERROR: {error}"
            if not where.is_dir():
                return f"ERROR: cwd is not a directory: {cwd}"
        try:
            task = manager.start(command, cwd=where, owner=me(), name=name)     # never a terminal (no ``kind``)
        except TaskLimit as limit:
            return str(limit)
        deadline = time.monotonic() + FIRST_OUTPUT_WAIT_SEC
        settle: Optional[float] = None
        while task.state == "running" and time.monotonic() < deadline:
            if settle is None and manager.output(task.id, 1).strip():
                settle = min(deadline, time.monotonic() + SETTLE_SEC)
            if settle is not None and time.monotonic() >= settle:
                break
            time.sleep(0.05)
        text = _clip(manager.output(task.id, START_LINES)).rstrip("\n")
        lines = [f"Started {task.id} in the background: {command}"]
        if task.state == "failed":
            lines += ["It could not start:", text or "(no output)"]
        elif task.state != "running":
            lines += [f"It exited at once with code {task.exit_code}:", text or "(no output)"]
        else:
            lines += ["Output so far:", text or "(no output yet)"]
        return "\n".join(lines)

    def background_output(id: str, lines: int = DEFAULT_LINES) -> str:
        task = manager.get(id)
        if task is None:
            return unknown(id)
        try:
            wanted = int(lines)
        except (TypeError, ValueError):
            wanted = DEFAULT_LINES
        return output_text(manager, task, max(1, min(wanted, MAX_LINES)))

    def stop_background(id: str) -> str:
        task = manager.get(id)
        if task is None:
            return unknown(id)
        if task.state != "running":
            return ended_text(task)
        mine = me()
        if task.kind == "terminal" or not mine or task.owner != mine:
            try:
                with approval_lock(gate):
                    refusal = gate.check("stop_other_task", {"command": f"stop {task.id}: {task.command}"})
            except Exception:                       # the gate failing must never let the stop through
                return GATE_FAILED
            if refusal is not None:
                return refusal
        return stop_text(manager, task)

    def list_background() -> str:
        return list_text(manager, me(), titles)

    def send_to_terminal(id: str, text: str) -> str:
        if manager.get(id) is None:
            return unknown(id)
        try:
            _, offset = manager.read_from(id, 0)
            manager.send(id, text)
        except (ValueError, KeyError) as error:
            return f"ERROR: {error.args[0] if error.args else error}"
        time.sleep(SEND_WAIT_SEC)
        try:
            new, _ = manager.read_from(id, offset)
        except KeyError:
            new = ""
        echoed = f"$ {text}\n"
        if new.startswith(echoed):
            new = new[len(echoed):]
        return f"Sent to {id}: {text}\nOutput since:\n" + (_clip(new).rstrip("\n") or "(no new output yet)")

    raw = [
        StructuredTool(func=start_background, args_schema=_StartArgs, name="start_background",
                       description=START_DESCRIPTION),
        StructuredTool(func=background_output, args_schema=_OutputArgs, name="background_output",
                       description=OUTPUT_DESCRIPTION),
        StructuredTool(func=stop_background, args_schema=_IdArgs, name="stop_background",
                       description=STOP_DESCRIPTION),
        StructuredTool(func=list_background, args_schema=_NoArgs, name="list_background",
                       description=LIST_DESCRIPTION),
        StructuredTool(func=send_to_terminal, args_schema=_SendArgs, name="send_to_terminal",
                       description=SEND_DESCRIPTION),
    ]
    return [gate_tool(tool, gate, permissions=permissions) for tool in raw]
