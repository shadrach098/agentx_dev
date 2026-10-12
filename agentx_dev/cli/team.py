"""Teams mode: a Supervisor with three helpers, started by the ``start_team_job`` tool.

The main chat agent calls ``start_team_job`` for a big job. This module builds the helpers and the Supervisor
for that one job, runs it to the end inside the tool call and returns one result string, so the conversation
already contains what the team did. It does no terminal I/O: progress goes out through a ``notify`` callback.
The roles steer the helpers; they are not a sandbox (``run_shell`` can write files). The gate, which every
helper tool goes through, is the boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from agentx_dev import AgentRunner, AgentType, Supervisor
from agentx_dev.Supervisor import _result_failed
from agentx_dev.Tools import StructuredTool

TEAM_TOOL_NAME = "start_team_job"
MAX_TASK_CHARS = 4000
MAX_WHY_CHARS = 500
MAX_RESULT_CHARS = 6000
HELPER_STEPS = 25
MAX_SUBTASKS = 6
MAX_QUESTIONS = 3
ROLE_MARK = "Team role:"
READ_TOOLS = ("read_path", "list_directory", "find_files", "grep")

Notify = Callable[[str, Dict[str, Any]], None]


@dataclass(frozen=True)
class Role:
    name: str
    description: str          # what the planner reads when it picks a helper
    tools: Tuple[str, ...]
    instructions: str         # what the helper itself reads


ROLES: Tuple[Role, ...] = (
    Role("explorer",
         "Reads and searches the project and cannot change anything. Use it to look things up, find code "
         "and summarise what exists.",
         READ_TOOLS,
         "You can only read: list, find, grep and read files. You cannot change anything, so report what you "
         "found, with file paths."),
    Role("coder",
         "Edits files (and can read and search). Use it to write or change code and documents. It cannot "
         "run commands.",
         READ_TOOLS + ("write_file", "edit_file"),
         "You change files with write_file and edit_file, after reading them. You cannot run commands, so say "
         "what you changed and what should be run to check it."),
    Role("runner",
         "Runs shell commands and Python (and can read and search). Use it to run tests, builds and scripts "
         "and report the output. It does not edit files.",
         READ_TOOLS + ("run_shell", "run_python"),
         "You run commands and code and report exactly what they printed, including failures. Do not edit "
         "project files; report what needs changing instead."),
)


def role_prompt(role: Role) -> str:
    return (f"\n\n{ROLE_MARK} you are the {role.name} helper on a team led by a supervisor. {role.instructions} "
            "Finish with a short report; the supervisor passes it on.")


def build_helpers(model: Any, tools: Sequence[Any], addendum: str) -> Dict[str, Tuple[str, AgentRunner]]:
    """One ``AgentRunner`` per role, each holding only its slice of ``tools`` (the already gated tools, picked by
    name), so a helper's calls go through the same gate as the main chat. ``addendum`` is the chat's own system
    addendum (the project instructions); the role text follows it."""
    by_name = {tool.name: tool for tool in tools}
    helpers: Dict[str, Tuple[str, AgentRunner]] = {}
    for role in ROLES:
        chosen = [by_name[name] for name in role.tools if name in by_name]
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=chosen,
                             system_addendum=(addendum or "") + role_prompt(role),
                             max_iterations=HELPER_STEPS, auto_cache=False, verbose=False,
                             parallel_tool_workers=1)
        helpers[role.name] = (role.description, runner)
    return helpers


def _first_line(text: Any, limit: int = 160) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def format_result(result: Any) -> str:
    """The one string the main agent gets back: the outcome, each step's fate, and the final answer."""
    lines = [f"Team job finished (outcome: {getattr(result, 'outcome', 'done')})."]
    subtasks = list(getattr(result, "subtasks", None) or [])
    if subtasks:
        lines.append("Steps:")
        for number, sub in enumerate(subtasks, 1):
            if getattr(sub, "skipped", False):
                state = "skipped"
            elif _result_failed(sub):
                state = "failed: " + _first_line(getattr(sub, "error", "") or getattr(sub, "outcome", ""), 80)
            else:
                state = "done"
            lines.append(f"{number}. {sub.agent} - {state}: {_first_line(sub.query)}")
    lines += ["", "Result:", str(getattr(result, "content", ""))]
    text = "\n".join(lines)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... (cut; {len(text) - MAX_RESULT_CHARS} more characters)"
    return text


def run_team_job(task: str, *, model: Any, tools: Sequence[Any], addendum: str, notify: Notify,
                 ask_user: Optional[Callable[[str], Optional[str]]] = None,
                 describe_error: Callable[[BaseException], str] = str) -> str:
    """Plan ``task``, run the helpers one at a time and return the result text. Progress goes to ``notify``.

    Ctrl+C (``KeyboardInterrupt``) and ``SystemExit`` propagate, with the Supervisor's stream closed; any other
    error becomes a short failure result for the main agent to read."""
    helpers = build_helpers(model, tools, addendum)
    supervisor = Supervisor(model=model, agents=helpers, max_subtasks=MAX_SUBTASKS, verbose=False,
                            max_subtask_retries=0, max_plan_retries=1, ask_user=ask_user,
                            max_questions=MAX_QUESTIONS)
    events = supervisor.stream(task)
    result = None
    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "plan":
                steps = [{"agent": str(step.get("agent", "")), "query": str(step.get("query", ""))}
                         for step in (event.get("plan") or []) if isinstance(step, dict)]
                notify("plan", {"steps": steps})
            elif kind == "dispatch":
                notify("dispatch", {"agent": str(event.get("agent", "")), "query": str(event.get("query", ""))})
            elif kind == "subtask_result":
                sub = event.get("result")
                if sub is not None and getattr(sub, "agent", "") != "__spawn__":
                    failed = bool(getattr(sub, "skipped", False)) or _result_failed(sub)
                    text = getattr(sub, "error", "") or getattr(sub, "content", "")
                    notify("result", {"agent": str(sub.agent), "ok": not failed, "text": _first_line(text)})
            elif kind == "completion":
                result = event.get("result")
    except Exception as error:                     # KeyboardInterrupt and SystemExit are not Exceptions
        return f"Team job failed ({type(error).__name__}): {describe_error(error)}"
    finally:
        events.close()
    if result is None:
        return "Team job ended without a result."
    return format_result(result)


class TeamArgs(BaseModel):
    task: str = Field(..., min_length=1, max_length=MAX_TASK_CHARS,
                      description="The whole job, with enough detail for a team that has not seen this "
                                  "conversation.")
    why: str = Field("", max_length=MAX_WHY_CHARS,
                     description="One sentence: why this job is too big to do alone.")


TOOL_DESCRIPTION = (
    "Hand a LARGE job to a team of helpers (an explorer that reads, a coder that edits files, a runner that "
    "runs commands and tests), led by a supervisor who plans the steps. Use it only for large, multi-part "
    "work: several files or areas, research plus changes plus tests. Do small and medium jobs yourself. The "
    "user is asked before it starts, it makes many model calls, and only one team job can run per user message. "
    "You get the supervisor's result back; then summarise it for the user or continue.")


def make_team_tool(run: Callable[[str, str], str]) -> StructuredTool:
    """The ``start_team_job`` tool; ``run(task, why)`` does the work. Wrap it with ``gate_tool`` before use."""
    def start_team_job(task: str, why: str = "") -> str:
        return run(task, why)

    return StructuredTool(func=start_team_job, args_schema=TeamArgs, name=TEAM_TOOL_NAME,
                          description=TOOL_DESCRIPTION)


def team_request(task: str) -> str:
    """The message ``/team <task>`` sends the main agent."""
    return (f"Call the {TEAM_TOOL_NAME} tool now to hand this job to the team, then tell me what the team "
            f"did: {task}")
