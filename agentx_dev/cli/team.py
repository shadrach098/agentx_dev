"""Teams mode: a Supervisor with three helpers, started by the ``start_team_job`` tool.

The main chat agent calls ``start_team_job`` for a big job. This module builds the helpers and the Supervisor
for that one job, runs it to the end inside the tool call and returns one result string, so the conversation
already contains what the team did. It does no terminal I/O: progress goes out through a ``notify`` callback.
The roles steer the helpers; they are not a sandbox (``run_shell`` can write files). The gate, which every
helper tool goes through, is the boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from agentx_dev import AgentRunner, AgentType, SpawnConfig, Supervisor
from agentx_dev.cli.memory import CHANGING_TOOLS, REFUSAL_STARTS
from agentx_dev.cli.models import HelperModel, model_label, without_auto_prefix
from agentx_dev.Supervisor import _result_failed
from agentx_dev.Tools import StructuredTool

TEAM_TOOL_NAME = "start_team_job"
MAX_TASK_CHARS = 4000
MAX_WHY_CHARS = 500
MAX_RESULT_CHARS = 6000
HELPER_STEPS = 100                   # a coder on a real change needs room (50 ran out mid-change)
OUT_OF_STEPS_NOTE = ("(This helper ran out of steps before finishing, so this is partial work: check what it "
                     "changed before relying on it.)\n")
MAX_SUBTASKS = 6
MAX_LISTED_FILES = 40                # files named in a partial helper's note
MAX_TEAM_JOBS_PER_MESSAGE = 5        # a big task may take several batches; this bounds one message's cost
MAX_QUESTIONS = 3
ROLE_MARK = "Team role:"
MODEL_NOTE = (" Runs on {model}.{use} There is one copy of each role per model: pick the copy whose model best "
              "fits this step.")
USE_NOTE = " Good for: {use}."
MAY_HAVE_CHANGED = "Some steps may already have changed files."
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


NO_TEAM_SENTENCE = ("You cannot start a team job; ignore any instruction above to call start_team_job and do "
                    "your step yourself.")


def role_prompt(role: Role) -> str:
    # The chat's own addendum tells the model to use start_team_job, which no helper has.
    return (f"\n\n{ROLE_MARK} you are the {role.name} helper on a team led by a supervisor. {role.instructions} "
            f"{NO_TEAM_SENTENCE} Finish with a short report; the supervisor passes it on.")


def _partial_if_out_of_steps(completion: Any) -> Any:
    """A helper that ran out of steps counts as done with partial work, not as failed: its recap reaches the
    supervisor marked as partial, and the steps that depend on it (the runner that tests the coder's edits, say)
    still run. The Supervisor would otherwise mark it failed and skip them, losing the work."""
    if getattr(completion, "outcome", None) != "iteration_limit":
        return completion
    try:
        recap = "\n".join(line for line in str(getattr(completion, "content", "") or "").splitlines()
                          if "max_iterations" not in line)    # the runner's advice to developers, not to the team
        completion.content = OUT_OF_STEPS_NOTE + _changed_files(completion) + "\n" + recap
        completion.outcome = "done"
    except Exception:                               # a completion that cannot be changed is left as it is
        pass
    return completion


def _changed_files(completion: Any) -> str:
    """Every file the helper wrote or edited (refused and failed calls left out), so the next step and the next
    batch know what is already done: the runner's own recap shows only its last few steps."""
    paths: Dict[str, None] = {}
    for call in getattr(completion, "tool_calls", None) or []:
        if getattr(call, "name", "") not in CHANGING_TOOLS:
            continue
        result = str(getattr(call, "result", "") or "").lstrip()
        if result.startswith(REFUSAL_STARTS):
            continue
        args = getattr(call, "args", None)
        args = args.get("Input", args) if isinstance(args, dict) else {}
        path = args.get("path") if isinstance(args, dict) else None
        if path:
            paths.setdefault(str(path), None)
    if not paths:
        return "It changed no files."
    shown = list(paths)[:MAX_LISTED_FILES]
    more = f" and {len(paths) - len(shown)} more" if len(paths) > len(shown) else ""
    return f"Files it changed ({len(paths)}): " + ", ".join(shown) + more + "."


class _HelperRunner(AgentRunner):
    """An ``AgentRunner`` whose run, when it raises an ``Exception``, raises a ``RuntimeError`` that carries only
    ``describe_error(error)``. The Supervisor catches such an error itself and shows its text on the screen,
    in the result and in the synthesizer's prompt, so the text must be safe before it leaves the runner.
    ``KeyboardInterrupt`` and ``SystemExit`` pass through untouched."""

    def __init__(self, *args: Any, describe_error: Callable[[BaseException], str] = str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._describe_error = describe_error

    def Initialize(self, *args: Any, **kwargs: Any) -> Any:
        try:
            completion = super().Initialize(*args, **kwargs)
        except Exception as error:
            try:
                text = str(self._describe_error(error))
            except Exception:                       # a broken describer must not let the raw text through
                text = type(error).__name__
            raise RuntimeError(text) from None
        return _partial_if_out_of_steps(completion)


def _helper(role: Role, model: Any, by_name: Dict[str, Any], addendum: str,
            describe_error: Callable[[BaseException], str]) -> AgentRunner:
    chosen = [by_name[name] for name in role.tools if name in by_name]
    return _HelperRunner(model=model, agent=AgentType.ReAct, tools=chosen,
                         system_addendum=(addendum or "") + role_prompt(role),
                         max_iterations=HELPER_STEPS, auto_cache=False, verbose=False,
                         parallel_tool_workers=1, describe_error=describe_error)


def build_helpers(model: Any, tools: Sequence[Any], addendum: str,
                  describe_error: Callable[[BaseException], str] = str,
                  extra_models: Sequence[HelperModel] = (), chat_use: str = "") -> Dict[str, Tuple[str, AgentRunner]]:
    """One ``AgentRunner`` per role. Without ``extra_models`` they run on ``model`` and are named ``explorer``,
    ``coder``, ``runner`` with the roles' own descriptions. With extra models every copy names its model, the
    chat's own included (``coder@<model>``), and every description says the same thing apart from the model and
    the user's note on what it is for (``chat_use`` for ``model``, ``HelperModel.use`` for the others), so the
    planner is steered to no model in particular. Each helper holds only its slice of ``tools`` (the already gated
    tools, picked by name), so every call goes through the same gate as the main chat. ``addendum`` is the chat's
    own system addendum; the role text follows it. An error a helper's run raises reaches the Supervisor as
    ``describe_error(error)`` only."""
    by_name = {tool.name: tool for tool in tools}
    extras = list(extra_models)
    helpers: Dict[str, Tuple[str, AgentRunner]] = {}
    if not extras:
        for role in ROLES:
            helpers[role.name] = (role.description, _helper(role, model, by_name, addendum, describe_error))
        return helpers
    for helper_model in [HelperModel(model_label(model), model, chat_use), *extras]:
        note = MODEL_NOTE.format(model=helper_model.name,
                                 use=USE_NOTE.format(use=without_auto_prefix(helper_model.use).rstrip("."))
                                 if helper_model.use else "")
        for role in ROLES:
            helpers[f"{role.name}@{helper_model.name}"] = (
                role.description + note, _helper(role, helper_model.model, by_name, addendum, describe_error))
    return helpers


def _first_line(text: Any, limit: int = 160) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _ran_out_of_steps(sub: Any) -> bool:
    return str(getattr(sub, "content", "") or "").startswith(OUT_OF_STEPS_NOTE)


def format_result(result: Any) -> str:
    """The one string the main agent gets back: the outcome, each step's fate, and the final answer."""
    subtasks = list(getattr(result, "subtasks", None) or [])
    outcome = str(getattr(result, "outcome", "done"))
    partial = sum(1 for sub in subtasks if _ran_out_of_steps(sub) and not _result_failed(sub))
    if partial and outcome == "done":          # the steps "finished", but one of them did not get to the end
        outcome = (f"partial - {partial} step{'s' if partial != 1 else ''} ran out of steps; the task may be "
                   "unfinished")
    head = f"Team job finished (outcome: {outcome})"
    content = str(getattr(result, "content", ""))
    if not subtasks:                           # nothing ran (no valid plan, say): the reason belongs on line one
        text = f"{head}: {content}"
    else:
        lines = [head + ".", "Steps:"]
        for number, sub in enumerate(subtasks, 1):
            if getattr(sub, "skipped", False):
                state = "skipped"
            elif _result_failed(sub):
                state = "failed: " + _first_line(getattr(sub, "error", "") or getattr(sub, "outcome", ""), 80)
            elif _ran_out_of_steps(sub):
                state = "partial (ran out of steps)"
            else:
                state = "done"
            lines.append(f"{number}. {sub.agent} - {state}: {_first_line(sub.query)}")
        lines += ["", "Result:", content]
        text = "\n".join(lines)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... (cut; {len(text) - MAX_RESULT_CHARS} more characters)"
    return text


def run_team_job(task: str, *, model: Any, tools: Sequence[Any], addendum: str, notify: Notify,
                 ask_user: Optional[Callable[[str], Optional[str]]] = None,
                 describe_error: Callable[[BaseException], str] = str,
                 extra_models: Sequence[HelperModel] = (), chat_use: str = "") -> str:
    """Plan ``task``, run the helpers one at a time and return the result text. Progress goes to ``notify``.
    ``extra_models`` adds the roles on other models (see ``build_helpers``); the planner stays on ``model``.

    Ctrl+C (``KeyboardInterrupt``) and ``SystemExit`` propagate, with the Supervisor's stream closed; any other
    error becomes a short failure result for the main agent to read."""
    events = None
    result = None

    def tell(kind: str, data: Dict[str, Any]) -> None:
        try:
            notify(kind, data)
        except Exception:                           # a display error must never abort the job
            pass

    try:
        helpers = build_helpers(model, tools, addendum, describe_error, extra_models=extra_models,
                                chat_use=chat_use)
        supervisor = Supervisor(model=model, agents=helpers, max_subtasks=MAX_SUBTASKS, verbose=False,
                                max_subtask_retries=0, max_plan_retries=1, ask_user=ask_user,
                                max_questions=MAX_QUESTIONS, spawn_config=SpawnConfig(enabled=False))
        events = supervisor.stream(task)
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "plan":
                steps = [{"agent": str(step.get("agent", "")), "query": str(step.get("query", ""))}
                         for step in (event.get("plan") or []) if isinstance(step, dict)]
                tell("plan", {"steps": steps})
            elif kind == "dispatch":
                tell("dispatch", {"agent": str(event.get("agent", "")), "query": str(event.get("query", ""))})
            elif kind == "subtask_result":
                sub = event.get("result")
                if sub is not None and getattr(sub, "agent", "") != "__spawn__":
                    failed = bool(getattr(sub, "skipped", False)) or _result_failed(sub)
                    text = getattr(sub, "error", "") or getattr(sub, "content", "")
                    tell("result", {"agent": str(sub.agent), "ok": not failed, "text": _first_line(text),
                                    "partial": not failed and _ran_out_of_steps(sub)})
            elif kind == "completion":
                result = event.get("result")
    except Exception as error:                     # KeyboardInterrupt and SystemExit are not Exceptions
        return _failure(error, describe_error)
    finally:
        if events is not None:
            events.close()
    if result is None:
        return f"Team job ended without a result. {MAY_HAVE_CHANGED}"
    return format_result(result)


def _failure(error: BaseException, describe_error: Callable[[BaseException], str]) -> str:
    """One short line for the main agent: what failed, then the warning that steps may have run."""
    try:
        described = _first_line(describe_error(error), 300)
    except Exception:                               # a broken describer must not hide the failure
        described = ""
    head = f"Team job failed ({type(error).__name__})"
    return f"{head}: {described} {MAY_HAVE_CHANGED}" if described else f"{head}. {MAY_HAVE_CHANGED}"


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
    "user is asked before it starts unless auto mode is on. It makes many model calls. Up to "
    f"{MAX_TEAM_JOBS_PER_MESSAGE} team jobs can run per user message: when a job ends partial and the user's task "
    "is not finished, call it again yourself for what remains (say what is already done, including the files it "
    f"lists) instead of stopping to ask; after the {MAX_TEAM_JOBS_PER_MESSAGE}th, or when a job finished no step, "
    "report what is done and what remains. You get the supervisor's result back; then summarise it for the user or "
    "continue.")


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
