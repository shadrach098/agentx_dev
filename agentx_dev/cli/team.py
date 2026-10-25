"""Teams mode: a Supervisor with three helpers, started by the ``start_team_job`` tool.

The main chat agent calls ``start_team_job`` for a big job. This module builds the helpers and the Supervisor
for that one job, runs it to the end inside the tool call and returns one result string, so the conversation
already contains what the team did. It does no terminal I/O: progress goes out through a ``notify`` callback.
Steps that only read (the explorer's) run up to ``PARALLEL_HELPERS`` at a time; a step that edits or runs commands
runs alone. The helpers run in worker threads; whatever draws on the screen or asks the user is handed to the
thread that started the job (``on_main``), so prompts never overlap. The roles steer the helpers; they are not a sandbox (``run_shell`` can write files). The gate, which every
helper tool goes through, is the boundary."""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from agentx_dev import AgentRunner, AgentType, AsyncSupervisor, SpawnConfig
from agentx_dev.cli.memory import CHANGING_TOOLS, REFUSAL_STARTS
from agentx_dev.cli.models import HelperModel, model_label, without_auto_prefix
from agentx_dev.Runner.Trim import TrimPolicy
from agentx_dev.Supervisor import SubtaskResult, _plan_uses_deps, _result_failed
from agentx_dev.Tools import StructuredTool

TEAM_TOOL_NAME = "start_team_job"
MAX_TASK_CHARS = 4000
MAX_WHY_CHARS = 500
MAX_RESULT_CHARS = 20_000            # the whole report reaches the chat (at 6,000 an audit lost most findings)
HELPER_STEPS = 100                   # a coder on a real change needs room (50 ran out mid-change)
# A helper sends its recent tool results again on every step: keep 20,000-40,000 characters of them whole, like the
# chat, moving the cut in steps of 20,000 so the provider's cache can reuse the rest. (At 12,000-24,000 helpers
# lost the files they had read and read them again, eight to ten times each.)
HELPER_TRIM = TrimPolicy(keep_last=1, keep_chars=40_000, step_chars=20_000)
PARALLEL_HELPERS = 3                 # read-only steps running at once
READ_ONLY_ROLES = ("explorer",)      # their steps may run side by side; every other step runs alone
FULL_READS_PER_FILE = 2              # whole reads of one unchanged file in a step; the next one is answered instead
MIN_STEP_TOKENS = 100_000            # a step's token share is the message's limit, and never less than this
STOP_WAIT_SEC = 5.0                  # after Ctrl+C, how long to wait for the job's thread to cancel its steps
REPEAT_READ_NOTE = ("Not read again: {path} was already read in full {count} times in this step and has not "
                    "changed since. Use what you found in it, grep it for the part you need, or read a part with "
                    "offset and limit.")
NOT_STARTED = "not started: the user chose to wrap up at the token limit"
STEP_BUDGET_NOTE = ("Not run: this step has used its share of the token limit ({used:,} of {share:,} tokens). Stop "
                    "calling tools and write your report now: what you found, with file paths, and what is left "
                    "undone.")
OUT_OF_STEPS_NOTE = ("(This helper ran out of steps before finishing, so this is partial work: check what it "
                     "changed before relying on it.)\n")
MAX_SUBTASKS = 6
MAX_LISTED_FILES = 40                # files named in a partial helper's note
RUNNER_ADVICE = ("Hit max_iterations (", "The agent likely needed more iterations")   # recap lines dropped
MAX_TEAM_JOBS_PER_MESSAGE = 5        # a big task may take several batches; this bounds one message's cost
MAX_QUESTIONS = 3
ROLE_MARK = "Team role:"
MODEL_NOTE = (" Runs on {model}.{use} There is one copy of each role per model: pick the copy whose model best "
              "fits this step.")
USE_NOTE = " Good for: {use}."
MAY_HAVE_CHANGED = "Some steps may already have changed files."
READ_TOOLS = ("read_path", "list_directory", "find_files", "grep")
WEB_TOOLS = ("web_search", "web_fetch")
# tools that cannot change a file: after any other call (a command, an MCP tool) a file read before may differ
QUIET_TOOLS = READ_TOOLS + WEB_TOOLS + ("use_skill", "recall")

Notify = Callable[[str, Dict[str, Any]], None]


class JobStopped(KeyboardInterrupt):
    """The team job is over (Ctrl+C, the token limit, or it ended) while a helper was still working: the helper
    stops at its next tool call or model reply, the way Ctrl+C stops a run."""


class _Job:
    """What every step of one team job shares: whether the job is over, and each step's token share."""

    def __init__(self, step_tokens: int = 0) -> None:
        self.stopped = False
        self.step_tokens = max(0, int(step_tokens or 0))


class _Step:
    """One helper step: whose it is, the tokens its model replies used, and the files it read whole."""

    def __init__(self, helper: str, job: _Job) -> None:
        self.helper = helper
        self.job = job
        self.tokens = 0
        self.reads: Dict[str, int] = {}


_local = threading.local()           # .step: the _Step this thread works for (None outside a team job)


def current_helper() -> str:
    """The helper whose step this thread is running ("" outside one); inside ``on_main`` too."""
    step = getattr(_local, "step", None)
    return step.helper if step is not None else ""


def note_usage(model: Any) -> None:
    """For the app's ``on_usage`` hook: count a helper's model reply against its step's token share, and stop a
    helper whose job is already over."""
    step = getattr(_local, "step", None)
    if step is None:
        return
    if step.job.stopped:
        raise JobStopped()
    usage = getattr(model, "usage", None)
    try:
        step.tokens += (int(getattr(usage, "last_input_tokens", 0) or 0)
                        + int(getattr(usage, "last_output_tokens", 0) or 0))
    except (TypeError, ValueError):
        pass


def step_token_share(token_limit: int) -> int:
    """The tokens one helper step may use before its tools tell it to report: the message's limit, at least
    ``MIN_STEP_TOKENS``; 0 (no share) when there is no limit."""
    try:
        limit = int(token_limit or 0)
    except (TypeError, ValueError):
        return 0
    return max(MIN_STEP_TOKENS, limit) if limit > 0 else 0


def _normal(path: Any) -> str:
    return os.path.normcase(os.path.normpath(path)) if isinstance(path, str) and path.strip() else ""


def _full_read(name: str, args: Dict[str, Any]) -> str:
    """The file a ``read_path`` call reads whole (normalised), or "" for a part of one or any other call."""
    if name != "read_path" or args.get("limit") is not None:
        return ""
    try:
        if int(args.get("offset") or 1) > 1:
            return ""
    except (TypeError, ValueError):
        return ""
    return _normal(args.get("path"))


def _note_call(step: _Step, name: str, args: Dict[str, Any], whole: str, result: Any) -> None:
    """After a call: count a whole read that worked; forget the reads of a file that may have changed."""
    try:
        text = str(result if result is not None else "").lstrip()
    except Exception:                               # a result that cannot be shown counts as a failure
        text = "ERROR"
    if whole:
        if not text.startswith(REFUSAL_STARTS):
            step.reads[whole] = step.reads.get(whole, 0) + 1
    elif name in CHANGING_TOOLS:
        step.reads.pop(_normal(args.get("path")), None)
    elif name not in QUIET_TOOLS:                   # a command or an MCP tool may change any file
        step.reads.clear()


def _step_tool(tool: Any, step: _Step) -> StructuredTool:
    """``tool`` for one step: it stops once the job is over; it answers instead of running once the step has used
    its token share, or for a third whole read of a file that has not changed; and while it runs the thread is
    marked as this helper's, so the screen and the prompts can name the helper."""
    name, inner = tool.name, tool.func

    def run(**kwargs: Any) -> Any:
        if step.job.stopped:
            raise JobStopped()
        share = step.job.step_tokens
        if share and step.tokens >= share:
            return STEP_BUDGET_NOTE.format(used=step.tokens, share=share)
        whole = _full_read(name, kwargs)
        if whole and step.reads.get(whole, 0) >= FULL_READS_PER_FILE:
            return REPEAT_READ_NOTE.format(path=str(kwargs.get("path"))[:200], count=step.reads[whole])
        previous, _local.step = getattr(_local, "step", None), step
        try:
            result = inner(**kwargs)
        finally:
            _local.step = previous
        _note_call(step, name, kwargs, whole, result)
        return result

    run.__name__, run.__qualname__, run.__module__ = name, f"team_step.{name}", __name__
    wrapped = StructuredTool(func=run, args_schema=tool.args_schema, name=name, description=tool.description)
    wrapped.cacheable = False            # a guard's answer must never be replayed as the tool's result
    for attribute in ("mcp_server", "mcp_remote"):
        if hasattr(tool, attribute):
            setattr(wrapped, attribute, getattr(tool, attribute))
    return wrapped


class _Pump:
    """Runs functions on the thread that started a team job, for the worker threads: whatever draws on the screen
    or asks the user happens there, one thing at a time, in the order it was handed over."""

    def __init__(self) -> None:
        self.owner = threading.current_thread()
        self._items: "queue.Queue[Tuple[Callable[..., Any], tuple, Any, Optional[Future]]]" = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], args: tuple, wait: bool) -> Any:
        future: Optional[Future] = Future() if wait else None
        with self._lock:
            if self._closed:
                if wait:
                    raise JobStopped()
                return None
            self._items.put((fn, args, getattr(_local, "step", None), future))
        return future.result() if future is not None else None

    def serve(self, finished: threading.Event) -> None:
        """Run what is handed over until ``finished`` is set and nothing is left. Ctrl+C propagates."""
        while True:
            try:
                fn, args, step, future = self._items.get(timeout=0.05)
            except queue.Empty:
                if finished.is_set():
                    return
                continue
            previous, _local.step = getattr(_local, "step", None), step
            try:
                value = fn(*args)
            except BaseException as error:          # noqa: BLE001 - handed to the waiting thread
                if future is not None:
                    future.set_exception(error)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                # nobody waits for a display call: its error must never abort the job
            else:
                if future is not None:
                    future.set_result(value)
            finally:
                _local.step = previous

    def close(self) -> None:
        """No more handing over; whoever still waits gets ``JobStopped``."""
        with self._lock:
            self._closed = True
        while True:
            try:
                _, _, _, future = self._items.get_nowait()
            except queue.Empty:
                return
            if future is not None and not future.done():
                future.set_exception(JobStopped())


_active: Optional[_Pump] = None
_active_lock = threading.Lock()


def on_main(fn: Callable[..., Any], *args: Any, wait: bool = True) -> Any:
    """``fn(*args)`` on the thread that started the running team job (``current_helper()`` there still names the
    caller's helper) and its result; ``wait=False`` hands it over and returns ``None`` at once. Outside a team job,
    or on that thread, it is a plain call. A helper thread whose job is over gets ``JobStopped``."""
    pump = _active
    here = threading.current_thread()
    step = getattr(_local, "step", None)
    if step is not None and step.job.stopped and (pump is None or here is not pump.owner):
        raise JobStopped()
    if pump is None or here is pump.owner:
        value = fn(*args)
        return value if wait else None
    return pump.submit(fn, args, wait)


@dataclass(frozen=True)
class Role:
    name: str
    description: str          # what the planner reads when it picks a helper
    tools: Tuple[str, ...]
    instructions: str         # what the helper itself reads


ROLES: Tuple[Role, ...] = (
    Role("explorer",
         "Reads and searches the project and the web (web_search, web_fetch) and cannot change anything. Use it "
         "to look things up, research online, find code and summarise what exists.",
         READ_TOOLS + WEB_TOOLS,
         "You can only read: list, find, grep and read files, and search the web and fetch pages. You cannot "
         "change anything, so report what you found, with file paths or page links."),
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
         "project files; report what needs changing instead. Read files with read_path and search with grep and "
         "find_files: do not print files with run_python or run_shell, which costs far more. Do not scrape web "
         "pages with Python or the shell: web research is the explorer's step."),
)


SKILLS_SENTENCE = ("The supervisor has already planned the work: open a skill only when your step names it or it "
                   "covers exactly this kind of step, not a general one about how to start or plan.")
NO_TEAM_SENTENCE = ("You cannot start a team job; ignore any instruction above to call start_team_job and do "
                    "your step yourself.")


def role_prompt(role: Role) -> str:
    # The chat's own addendum tells the model to use start_team_job, which no helper has.
    return (f"\n\n{ROLE_MARK} you are the {role.name} helper on a team led by a supervisor. {role.instructions} "
            f"{NO_TEAM_SENTENCE} {SKILLS_SENTENCE} Finish with a short report; the supervisor passes it on.")


def _partial_if_out_of_steps(completion: Any) -> Any:
    """A helper that ran out of steps counts as done with partial work, not as failed: its recap reaches the
    supervisor marked as partial, and the steps that depend on it (the runner that tests the coder's edits, say)
    still run. The Supervisor would otherwise mark it failed and skip them, losing the work."""
    if getattr(completion, "outcome", None) != "iteration_limit":
        return completion
    try:
        recap = "\n".join(line for line in str(getattr(completion, "content", "") or "").splitlines()
                          if not line.startswith(RUNNER_ADVICE))   # the runner's advice to developers, not the team
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
    """The ``AgentRunner`` a helper is registered as. Each step runs on a fresh ``AgentRunner`` made with the same
    settings, so two steps of one helper can run at once, each with its own ``_Step`` (its tools wrapped by
    ``_step_tool``) under ``job`` (set by ``run_team_job``; a job of its own otherwise).

    When the run raises an ``Exception``, a ``RuntimeError`` that carries only ``describe_error(error)`` is raised
    instead. The Supervisor catches such an error itself and shows its text on the screen, in the result and in the
    synthesizer's prompt, so the text must be safe before it leaves the runner. ``KeyboardInterrupt`` and
    ``SystemExit`` pass through untouched."""

    def __init__(self, *args: Any, describe_error: Callable[[BaseException], str] = str, helper_name: str = "",
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._describe_error = describe_error
        self._settings = (args, kwargs)
        self.helper_name = helper_name
        self.job: Optional[_Job] = None

    def Initialize(self, *args: Any, **kwargs: Any) -> Any:
        step = _Step(self.helper_name, self.job or _Job())
        settings_args, settings = self._settings
        previous, _local.step = getattr(_local, "step", None), step
        try:
            runner = AgentRunner(*settings_args, **{**settings, "tools": [_step_tool(tool, step) for tool in
                                                                           settings.get("tools") or []]})
            completion = runner.Initialize(*args, **kwargs)
        except Exception as error:
            try:
                text = str(self._describe_error(error))
            except Exception:                       # a broken describer must not let the raw text through
                text = type(error).__name__
            raise RuntimeError(text) from None
        finally:
            _local.step = previous
        return _partial_if_out_of_steps(completion)


def _helper(role: Role, model: Any, by_name: Dict[str, Any], addendum: str,
            describe_error: Callable[[BaseException], str], skills: Any = None,
            name: str = "") -> AgentRunner:
    chosen = [by_name[name] for name in role.tools if name in by_name]
    # The user's MCP tools go to every helper: what they do is up to the server, and each asks before it runs.
    chosen += [tool for tool in by_name.values() if getattr(tool, "mcp_server", None) and tool not in chosen]
    return _HelperRunner(model=model, agent=AgentType.ReAct, tools=chosen,
                         system_addendum=(addendum or "") + role_prompt(role),
                         max_iterations=HELPER_STEPS, auto_cache=False, verbose=False,
                         parallel_tool_workers=1, describe_error=describe_error, skills=skills,
                         trim_results=HELPER_TRIM, helper_name=name or role.name)


def build_helpers(model: Any, tools: Sequence[Any], addendum: str,
                  describe_error: Callable[[BaseException], str] = str,
                  extra_models: Sequence[HelperModel] = (), chat_use: str = "",
                  skills: Any = None) -> Dict[str, Tuple[str, AgentRunner]]:
    """One ``AgentRunner`` per role. Without ``extra_models`` they run on ``model`` and are named ``explorer``,
    ``coder``, ``runner`` with the roles' own descriptions. With extra models every copy names its model, the
    chat's own included (``coder@<model>``), and every description says the same thing apart from the model and
    the user's note on what it is for (``chat_use`` for ``model``, ``HelperModel.use`` for the others), so the
    planner is steered to no model in particular. Each helper holds only its slice of ``tools`` (the already gated
    tools, picked by name), so every call goes through the same gate as the main chat. ``addendum`` is the chat's
    own system addendum; the role text follows it. ``skills`` (a ``Skills``) gives every helper ``use_skill`` and the
    list of the skills that fit its role's tools, so a step that says "use the X skill" can be done. An error a
    helper's run raises reaches the Supervisor as ``describe_error(error)`` only."""
    by_name = {tool.name: tool for tool in tools}
    extras = list(extra_models)
    helpers: Dict[str, Tuple[str, AgentRunner]] = {}
    if not extras:
        for role in ROLES:
            helpers[role.name] = (role.description,
                                  _helper(role, model, by_name, addendum, describe_error, skills, role.name))
        return helpers
    for helper_model in [HelperModel(model_label(model), model, chat_use), *extras]:
        note = MODEL_NOTE.format(model=helper_model.name,
                                 use=USE_NOTE.format(use=without_auto_prefix(helper_model.use).rstrip("."))
                                 if helper_model.use else "")
        for role in ROLES:
            name = f"{role.name}@{helper_model.name}"
            helpers[name] = (role.description + note,
                             _helper(role, helper_model.model, by_name, addendum, describe_error, skills, name))
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


def _read_only(agent: Any) -> bool:
    return str(agent or "").split("@", 1)[0] in READ_ONLY_ROLES


def _ordered(plan: List[Any]) -> List[Any]:
    """A plan without ``depends_on`` runs in order, each step seeing every earlier step's findings. That stays,
    except that read-only steps next to each other run side by side: each step depends on every step before its run
    of read-only steps. A plan that declares its own dependencies is left as it is."""
    if _plan_uses_deps(plan):
        return plan
    out: List[Any] = []
    before: List[str] = []
    together: List[str] = []
    reading = False
    for index, item in enumerate(plan):
        if not isinstance(item, dict):
            out.append(item)
            continue
        step = dict(item)
        step_id = str(step.get("id") or f"step_{index + 1}")
        step["id"] = step_id
        reads = _read_only(step.get("agent"))
        if not (reads and reading):
            before, together = before + together, []
        step["depends_on"] = list(before)
        together.append(step_id)
        reading = reads
        out.append(step)
    return out


class _StepLock:
    """Read-only steps share it; any other step holds it alone (no reads beside an edit or a command)."""

    def __init__(self) -> None:
        self._free = asyncio.Condition()
        self._readers = 0
        self._writer = False

    @asynccontextmanager
    async def hold(self, alone: bool) -> AsyncIterator[None]:
        async with self._free:
            if alone:
                await self._free.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            else:
                await self._free.wait_for(lambda: not self._writer)
                self._readers += 1
        try:
            yield
        finally:
            async with self._free:
                if alone:
                    self._writer = False
                else:
                    self._readers -= 1
                self._free.notify_all()


class TeamSupervisor(AsyncSupervisor):
    """The framework's ``AsyncSupervisor`` with the team's rules: read-only steps run side by side (up to
    ``max_parallel``), any other step alone, and a plan without dependencies keeps its order (``_ordered``).
    ``on_start(agent, query)`` is called when a step really starts; the framework's own ``dispatch`` events, which
    announce every step up front, are dropped. Once ``closed()`` is true (the user said to wrap up), a step that has
    not started is not started: it ends at once as skipped, and the job reports with what it has."""

    def __init__(self, *args: Any, on_start: Optional[Callable[[str, str], None]] = None,
                 closed: Optional[Callable[[], Any]] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_start = on_start
        self._closed = closed
        self._step_lock: Optional[_StepLock] = None

    async def _run_plan(self, plan: List[dict], *args: Any, **kwargs: Any):
        async with aclosing(super()._run_plan(_ordered(plan), *args, **kwargs)) as events:
            async for event in events:
                if isinstance(event, dict) and event.get("type") == "dispatch":
                    continue
                yield event

    def _wrapping_up(self) -> bool:
        try:
            return bool(self._closed is not None and self._closed())
        except Exception:                           # a broken check must not stop the job
            return False

    async def _run_subtask(self, agent_name: str, sub_query: str, prior_results: Any = None,
                           budget: Any = None) -> Any:
        if self._step_lock is None:
            self._step_lock = _StepLock()
        async with self._step_lock.hold(alone=not _read_only(agent_name)):
            if self._wrapping_up():
                return SubtaskResult(agent=agent_name, query=sub_query, content="", error=NOT_STARTED, skipped=True)
            if self._on_start is not None:
                try:
                    self._on_start(agent_name, sub_query)
                except Exception:                   # a display error must never stop the step
                    pass
            return await super()._run_subtask(agent_name, sub_query, prior_results=prior_results, budget=budget)


def _run_on_loop(make: Callable[[], Any], job: _Job, pump: _Pump) -> Any:
    """Run the coroutine ``make()`` on an event loop in a new thread while this thread serves ``pump``, and return
    its value or raise its error here. Ctrl+C here cancels it. Either way the job is over when this returns, so a
    helper still working in a worker thread ends at its next tool call or model reply."""
    global _active
    finished = threading.Event()
    box: Dict[str, Any] = {}

    def target() -> None:
        loop = asyncio.new_event_loop()
        workers = ThreadPoolExecutor(max_workers=PARALLEL_HELPERS + 4, thread_name_prefix="agentx-helper")
        loop.set_default_executor(workers)
        try:
            task = loop.create_task(make())
            box["cancel"] = lambda: loop.call_soon_threadsafe(task.cancel)
            box["value"] = loop.run_until_complete(task)
        except BaseException as error:              # noqa: BLE001 - raised again on the waiting thread
            box["error"] = error
        finally:
            job.stopped = True
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except BaseException:                   # noqa: BLE001 - cleaning up must not hide the job's own end
                pass
            workers.shutdown(wait=False, cancel_futures=True)
            loop.close()
            finished.set()

    with _active_lock:
        _active = pump
    thread = threading.Thread(target=target, name="agentx-team", daemon=True)
    try:
        thread.start()
        pump.serve(finished)
    except BaseException:
        job.stopped = True
        cancel = box.get("cancel")
        if cancel is not None:
            try:
                cancel()
            except Exception:                       # the loop already ended
                pass
        raise
    finally:
        pump.close()
        with _active_lock:
            if _active is pump:
                _active = None
        thread.join(STOP_WAIT_SEC)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def run_team_job(task: str, *, model: Any, tools: Sequence[Any], addendum: str, notify: Notify,
                 ask_user: Optional[Callable[[str], Optional[str]]] = None,
                 describe_error: Callable[[BaseException], str] = str,
                 extra_models: Sequence[HelperModel] = (), chat_use: str = "", skills: Any = None,
                 step_tokens: int = 0, closed: Optional[Callable[[], Any]] = None) -> str:
    """Plan ``task``, run the helpers (read-only steps side by side, any other step alone) and return the result
    text. Progress goes to ``notify`` and questions to ``ask_user``, both on the calling thread. ``extra_models``
    adds the roles on other models (see ``build_helpers``); the planner stays on ``model``. ``step_tokens`` is each
    step's token share (see ``step_token_share``; 0 is none). Once ``closed()`` is true no further step starts.

    Ctrl+C (``KeyboardInterrupt``) and ``SystemExit`` propagate, with the Supervisor's stream closed; any other
    error becomes a short failure result for the main agent to read."""
    job = _Job(step_tokens)
    pump = _Pump()

    def tell(kind: str, data: Dict[str, Any]) -> None:
        on_main(notify, kind, data, wait=False)     # an error in it is dropped: a display error never aborts

    asker = (lambda question: on_main(ask_user, question)) if callable(ask_user) else ask_user

    async def consume() -> Any:
        helpers = build_helpers(model, tools, addendum, describe_error, extra_models=extra_models,
                                chat_use=chat_use, skills=skills)
        for _, runner in helpers.values():
            runner.job = job
        supervisor = TeamSupervisor(
            model=model, agents=helpers, max_subtasks=MAX_SUBTASKS, verbose=False, max_subtask_retries=0,
            max_plan_retries=1, ask_user=asker, max_questions=MAX_QUESTIONS, spawn_config=SpawnConfig(enabled=False),
            max_parallel=PARALLEL_HELPERS, closed=closed,
            on_start=lambda agent, query: tell("dispatch", {"agent": str(agent), "query": str(query)}))
        result = None
        async with aclosing(supervisor.astream(task)) as events:
            async for event in events:
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "plan":
                    steps = [{"agent": str(step.get("agent", "")), "query": str(step.get("query", ""))}
                             for step in (event.get("plan") or []) if isinstance(step, dict)]
                    tell("plan", {"steps": steps})
                elif kind == "subtask_result":
                    sub = event.get("result")
                    if sub is not None and getattr(sub, "agent", "") != "__spawn__":
                        failed = bool(getattr(sub, "skipped", False)) or _result_failed(sub)
                        text = getattr(sub, "error", "") or getattr(sub, "content", "")
                        tell("result", {"agent": str(sub.agent), "ok": not failed, "text": _first_line(text),
                                        "partial": not failed and _ran_out_of_steps(sub)})
                elif kind == "completion":
                    result = event.get("result")
        return result

    try:
        result = _run_on_loop(consume, job, pump)
    except Exception as error:                     # KeyboardInterrupt and SystemExit are not Exceptions
        return _failure(error, describe_error)
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
