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
import contextvars
import itertools
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from agentx_dev import AgentRunner, AgentType, AsyncSupervisor, SpawnConfig
from agentx_dev.cli.memory import CHANGING_TOOLS, REFUSAL_STARTS
from agentx_dev.cli.models import HelperModel, model_label, without_auto_prefix
from agentx_dev.cli.ui import summarize_result
from agentx_dev.Runner.Trim import TrimPolicy
from agentx_dev.SubAgents import AgentSpec
from agentx_dev.Supervisor import Specialist, SubtaskResult, _plan_uses_deps, _result_failed, _step_agent_name
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
# Input the provider serves from its prompt cache is billed at about a tenth (OpenAI's GPT-5 models, Anthropic's
# cache reads), and a helper's requests are mostly that: the limit and the shares count it as a tenth.
CACHED_SHARE = 10
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
MAX_NEW_HELPERS = 6                  # helpers the planner may define for one job
NEW_HELPER_DESCRIPTION_CHARS = 300
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
READING_TOOLS = frozenset(READ_TOOLS + WEB_TOOLS + ("recall",))   # a new helper with only these runs beside readers
NOT_FOR_NEW_HELPERS = frozenset({"delete_path", "move_file", "start_team_job", "delegate", "use_skill", "ask_user"})
NEW_HELPERS_NOTE = (
    "\n\nBesides these helpers you may make a helper for a step (see CREATING NEW SPECIALISTS below) when the step "
    "needs a focused role: give it a short name for its job (security-reviewer, payments-auditor, test-writer), "
    "instructions that say what it must find and report, and only the tools it needs from the menu. A helper whose "
    "tools only read, search or fetch runs beside other reading steps; one that edits files or runs commands runs "
    "alone. Its tool calls go through the same approvals as every other helper's.")
# Every step gets a role name for the screen: "git-checker@claude-sonnet-5-5" says what the helper is doing, where
# "runner@claude-sonnet-5-5" only said which built-in helper ran it.
ROLE_NAME_NOTE = (
    '\n\nGive every step an "as" field: a short name for what its helper does in this job, lowercase words joined '
    'by hyphens, for example "git-checker", "payments-fixer", "migration-verifier", "test-runner". It is the name '
    "the user sees for that step; it does not change which helper runs it. Do not reuse one name for two steps.")
_ROLE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_ROLE_NAME_CHARS = 32


def role_name(value: Any) -> str:
    """A step's ``as`` name, made safe for the screen: lowercase letters, digits and hyphens, at most
    ``MAX_ROLE_NAME_CHARS``; "" when nothing usable is left."""
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")[:MAX_ROLE_NAME_CHARS].strip("-")
    return text if _ROLE_NAME.match(text) else ""


NEW_HELPER_MODELS_NOTE = (
    ' To run it on another model, add "model" to its new_agent, one of: {models}. Without "model" it runs on '
    "{chat}. Pick by what the step needs; no model is preferred.")

Notify = Callable[[str, Dict[str, Any]], None]


class JobStopped(KeyboardInterrupt):
    """The team job is over (Ctrl+C, the token limit, or it ended) while a helper was still working: the helper
    stops at its next tool call or model reply, the way Ctrl+C stops a run."""


class _Job:
    """What every step of one team job shares: whether the job is over, and each step's token share."""

    def __init__(self, step_tokens: int = 0) -> None:
        self.stopped = False
        self.step_tokens = max(0, int(step_tokens or 0))


class StepStatus:
    """What the screen shows of one team step while it runs and after: the helper, a short label, when it started
    and ended, the tokens its model replies used, its tool calls by name, the files it read, the skills it opened,
    and a log of every call (``(tool, args, one-line summary, failed)``) for ``/team log``. The helper's threads update
    it; ``snapshot`` reads it safely from another thread."""

    def __init__(self, slot: int, helper: str, label: str = "", query: str = "") -> None:
        self.slot = slot
        self.helper = helper
        self.label = label or helper
        self.query = query
        self.started = time.monotonic()
        self.ended: Optional[float] = None
        self.tokens = 0                              # input and output its model replies reported
        self.cached = 0                              # the part of that input served from the provider's cache
        self.tool_uses = 0
        self.calls: Dict[str, int] = {}
        self.files: Dict[str, None] = {}
        self.skills: Dict[str, None] = {}
        self.log: List[Tuple[str, Dict[str, Any], str, bool]] = []
        self._lock = threading.Lock()

    def note_call(self, name: str, args: Dict[str, Any], result: Any) -> None:
        try:
            summary, failed = summarize_result(name, str(result if result is not None else ""))
        except Exception:                           # a result that cannot be summarised is still counted
            summary, failed = "", False
        text = str(result if result is not None else "").lstrip()
        failed = failed or text.startswith(REFUSAL_STARTS) or text.startswith(("Not read again", "Not run"))
        with self._lock:
            self.tool_uses += 1
            self.calls[name] = self.calls.get(name, 0) + 1
            if name == "read_path" and isinstance(args.get("path"), str):
                self.files[args["path"]] = None
            self.log.append((name, dict(args), text.splitlines()[0][:200] if failed and text else summary, failed))

    def note_skill(self, name: str) -> None:
        with self._lock:
            self.tool_uses += 1
            self.skills[str(name)] = None
            self.log.append(("use_skill", {"name": str(name)}, "", False))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"calls": dict(self.calls), "files": dict(self.files), "skills": list(self.skills),
                    "tokens": self.tokens, "cached": self.cached, "tool_uses": self.tool_uses,
                    "seconds": self.seconds(),
                    "log": list(self.log)}

    def seconds(self) -> float:
        return (self.ended if self.ended is not None else time.monotonic()) - self.started


# the StepStatus of the step a task (and, through asyncio.to_thread, its worker thread) is running
_running_status: "contextvars.ContextVar[Optional[StepStatus]]" = contextvars.ContextVar("agentx_team_step",
                                                                                         default=None)


class _Step:
    """One helper step: whose it is, the tokens its model replies used, the files it read whole, and what the
    screen shows of it (``status``)."""

    def __init__(self, helper: str, job: _Job, status: Optional[StepStatus] = None) -> None:
        self.helper = helper
        self.job = job
        self.tokens = 0
        self.reads: Dict[str, int] = {}
        self.status = status if status is not None else StepStatus(0, helper)


_local = threading.local()           # .step: the _Step this thread works for (None outside a team job)


def current_helper() -> str:
    """The helper whose step this thread is running ("" outside one); inside ``on_main`` too."""
    step = getattr(_local, "step", None)
    return step.helper if step is not None else ""


def counted_tokens(tokens_in: Any, tokens_out: Any, cached: Any = 0) -> int:
    """Tokens as the limit counts them: input and output, with input from the provider's cache counted as a tenth
    (about what it is billed at)."""
    try:
        tokens_in, tokens_out = max(0, int(tokens_in or 0)), max(0, int(tokens_out or 0))
        cached = min(max(0, int(cached or 0)), tokens_in)
    except (TypeError, ValueError):
        return 0
    return tokens_in - cached + cached // CACHED_SHARE + tokens_out


def current_status() -> Optional[StepStatus]:
    """The ``StepStatus`` of the step this thread is running (None outside one); inside ``on_main`` too."""
    step = getattr(_local, "step", None)
    return step.status if step is not None else None


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
        tokens_in = int(getattr(usage, "last_input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "last_output_tokens", 0) or 0)
        cached = min(int(getattr(usage, "last_cache_read_tokens", 0) or 0), tokens_in)
    except (TypeError, ValueError):
        return
    step.tokens += counted_tokens(tokens_in, tokens_out, cached)      # what the step's share is measured in
    step.status.tokens += tokens_in + tokens_out                       # what the screen shows, with the cached part
    step.status.cached += cached


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
            answer = STEP_BUDGET_NOTE.format(used=step.tokens, share=share)
            step.status.note_call(name, kwargs, answer)
            return answer
        whole = _full_read(name, kwargs)
        if whole and step.reads.get(whole, 0) >= FULL_READS_PER_FILE:
            answer = REPEAT_READ_NOTE.format(path=str(kwargs.get("path"))[:200], count=step.reads[whole])
            step.status.note_call(name, kwargs, answer)
            return answer
        previous, _local.step = getattr(_local, "step", None), step
        try:
            result = inner(**kwargs)
        finally:
            _local.step = previous
        _note_call(step, name, kwargs, whole, result)
        step.status.note_call(name, kwargs, result)
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
        status = _running_status.get()
        step = _Step(self.helper_name, self.job or _Job(), status)
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
            name: str = "", every_mcp_tool: bool = True) -> AgentRunner:
    chosen = [by_name[tool] for tool in role.tools if tool in by_name]
    if every_mcp_tool:
        # The user's MCP tools go to every built-in helper: what they do is up to the server, and each asks first.
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


def step_label(step: Dict[str, Any]) -> str:
    """A short name for a plan step: its id in words ("security_audit" -> "security audit"), or the first words of
    its task when the id says nothing ("step_3", "s1")."""
    raw = str(step.get("id") or "").strip()
    words = raw.replace("_", " ").replace("-", " ").strip()
    if words and not (len(words) <= 3 or words.split()[0] in ("step", "s") and words.split()[-1].isdigit()):
        return words[:40]
    query = " ".join(str(step.get("query") or "").split()[:5])
    return (query[:40] + "…") if len(query) > 40 else (query or raw or "step")


def _with_role(shown: str, role: str) -> str:
    """``runner@claude-sonnet-5-5`` with role ``git-checker``: ``git-checker@claude-sonnet-5-5``."""
    if not role:
        return shown
    _, at, model = shown.partition("@")
    return f"{role}{at}{model}"


def _plan_step(step: Dict[str, Any], shown_name: Optional[Callable[[Dict[str, Any]], str]] = None) -> Dict[str, Any]:
    """A plan step for the screen: who runs it (a new helper with its model, ``shown_name(step)``), what it is
    asked, its short name, and for a helper the planner made, the tools it asked for (``new``)."""
    who = shown_name(step) if shown_name is not None else str(_step_agent_name(step) or "")
    shown: Dict[str, Any] = {"agent": who, "query": str(step.get("query", "")), "label": step_label(step)}
    spec = step.get("new_agent")
    if isinstance(spec, dict):
        tools = spec.get("tools") if isinstance(spec.get("tools"), list) else []
        shown["new"] = [str(tool) for tool in tools if str(tool) not in NOT_FOR_NEW_HELPERS]
    return shown


def _read_only(agent: Any) -> bool:
    return str(agent or "").split("@", 1)[0] in READ_ONLY_ROLES


def _ordered(plan: List[Any], reads_only: Callable[[Any], bool] = None) -> List[Any]:  # type: ignore[assignment]
    """A plan without ``depends_on`` runs in order, each step seeing every earlier step's findings. That stays,
    except that read-only steps next to each other run side by side: each step depends on every step before its run
    of read-only steps. A plan that declares its own dependencies is left as it is."""
    if _plan_uses_deps(plan):
        return plan
    reads_only = reads_only or _read_only
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
        reads = reads_only(_step_agent_name(step))
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
    """The framework's ``AsyncSupervisor`` with the team's rules, through its public extension points only:

    - ``prepare_plan``: a plan without dependencies keeps its order, with reading steps next to each other side by
      side (``_ordered``); each step's short name is noted;
    - ``run_step``: reading steps share a lock, any other step holds it alone; once ``closed()`` is true (the user
      said to wrap up) a step that has not started is skipped; otherwise ``on_start(agent, query, status)`` is called
      and the step's ``StepStatus`` is set in the step's context, which its helper's thread inherits;
    - ``spawn_specialist``: a helper the planner defines is built by ``make_helper`` from the gated tools, on the
      model it asked for when the user allows that model;
    - ``planner_note``: the planner is told it may make helpers, and on which models."""

    def __init__(self, *args: Any, on_start: Optional[Callable[[str, str, StepStatus], None]] = None,
                 closed: Optional[Callable[[], Any]] = None,
                 make_helper: Optional[Callable[[str, str, List[str], str], Any]] = None,
                 models: Sequence[Tuple[str, str]] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_start = on_start
        self._closed = closed
        # make_helper(name, instructions, tool names, model) builds a helper the planner defined; None: no new helpers
        self._make_helper = make_helper
        # (model, what it is good for) a new helper may run on, the chat's first; one model: no choice offered
        self._models = [(str(name), str(use or "")) for name, use in models]
        self._asked_models: Dict[str, str] = {}          # new helper -> the model the planner asked for
        self._new_helpers: Dict[str, bool] = {}          # name -> whether it only reads
        self._planned_readers: set = set()
        self._step_lock: Optional[_StepLock] = None
        self._labels: Dict[Tuple[str, str], str] = {}
        self._roles: Dict[Tuple[str, str], str] = {}      # (agent, query) -> the role name the plan gave its step
        self._slots = itertools.count(1)
        self._statuses: Dict[int, StepStatus] = {}        # id(SubtaskResult) -> the status of the step that made it
        if self._make_helper is not None and self.spawn_config.enabled:
            note = NEW_HELPERS_NOTE
            if len(self._models) > 1:
                listed = ", ".join(f'"{name}"' + (f" (good for: {use})" if use else "") for name, use in self._models)
                note += NEW_HELPER_MODELS_NOTE.format(models=listed, chat=self._models[0][0])
            self.planner_note = (self.planner_note or "") + note
        self.planner_note = (self.planner_note or "") + ROLE_NAME_NOTE

    # -- names and models -------------------------------------------------------------------------------------

    def _reads_only(self, agent: Any) -> bool:
        name = str(agent or "")
        if name in self._new_helpers:
            return self._new_helpers[name]
        return _read_only(name) or name in self._planned_readers

    def _allowed_model(self, asked: Any) -> str:
        """The model a new helper runs on: ``asked`` when the user allows it, else the chat's ("" with one model)."""
        if not self._models:
            return ""
        wanted = str(asked or "").strip().lower()
        return next((model for model, _ in self._models if model.lower() == wanted), self._models[0][0])

    def model_for(self, name: Any) -> str:
        return self._allowed_model(self._asked_models.get(str(name), ""))

    def shown_name(self, name: Any) -> str:
        """A new helper's name on the screen: ``security-reviewer@gpt-6-luna`` when there is a model to tell."""
        name = str(name or "")
        return f"{name}@{self.model_for(name)}" if name in self._new_helpers and len(self._models) > 1 else name

    def shown_step_name(self, step: Dict[str, Any]) -> str:
        """Who runs a plan step, for the plan on the screen (shown before the plan runs): its role name when the plan
        gave one (``git-checker@claude-sonnet-5-5``), else the helper's."""
        name = str(_step_agent_name(step) or "")
        spec = step.get("new_agent")
        if isinstance(spec, dict) and len(self._models) > 1:
            name = f"{name}@{self._allowed_model(spec.get('model'))}"
        return _with_role(name, role_name(step.get("as")))

    def shown_for(self, agent: Any, query: Any) -> str:
        """A running step's name: its role name, with the helper's model when there is one to tell."""
        return _with_role(self.shown_name(agent), self._roles.get((str(agent), str(query)), ""))

    def label_of(self, agent: Any, query: Any) -> str:
        return self._labels.get((str(agent), str(query))) or step_label({"query": query})

    def status_of(self, result: Any) -> Optional[StepStatus]:
        return self._statuses.get(id(result))

    # -- the public extension points ---------------------------------------------------------------------------

    def prepare_plan(self, plan: List[dict]) -> List[dict]:
        for item in plan:
            if not isinstance(item, dict):
                continue
            key = (str(_step_agent_name(item) or ""), str(item.get("query", "")))
            self._labels[key] = step_label(item)
            if role_name(item.get("as")):
                self._roles[key] = role_name(item.get("as"))
            spec = item.get("new_agent")
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                tools = spec.get("tools") or []
                if isinstance(tools, list) and all(tool in READING_TOOLS for tool in tools):
                    self._planned_readers.add(spec["name"])
        return _ordered(plan, self._reads_only)

    def spawn_specialist(self, spec: AgentSpec) -> Tuple[Optional[str], str]:
        """A helper the planner defined: built the way the built-in ones are (the same gated tools, step wrapping,
        error redaction, steps and trimming), with only the tools it asked for out of ``spawn_config.tools``."""
        if self._make_helper is None:
            return None, "new helpers are off"
        name = spec.name
        if name in self._new_helpers:
            return name, ""                             # defined by an earlier step: the same helper
        if name in self.agents:
            return None, f"the name {name!r} belongs to a built-in helper"
        if len(self._new_helpers) >= MAX_NEW_HELPERS:
            return None, f"at most {MAX_NEW_HELPERS} new helpers per job"
        pool = {getattr(tool, "name", None) for tool in (self.spawn_config.tools or [])}
        granted = [tool for tool in spec.tools if tool in pool and tool not in NOT_FOR_NEW_HELPERS]
        self._asked_models[name] = spec.model
        try:
            runner = self._make_helper(name, spec.instructions, granted, self.model_for(name))
        except Exception as error:                      # a helper that cannot be built: the step fails, the job goes on
            return None, f"could not build it ({type(error).__name__})"
        self.agents[name] = Specialist(description=spec.instructions[:NEW_HELPER_DESCRIPTION_CHARS], runner=runner)
        self._new_helpers[name] = all(tool in READING_TOOLS for tool in granted)
        return name, ""

    def _wrapping_up(self) -> bool:
        try:
            return bool(self._closed is not None and self._closed())
        except Exception:                           # a broken check must not stop the job
            return False

    async def run_step(self, agent_name: str, query: str, run: Callable[[], Any]) -> Any:
        if self._step_lock is None:
            self._step_lock = _StepLock()
        async with self._step_lock.hold(alone=not self._reads_only(agent_name)):
            if self._wrapping_up():
                return SubtaskResult(agent=agent_name, query=query, content="", error=NOT_STARTED, skipped=True)
            status = StepStatus(next(self._slots), self.shown_for(agent_name, query), self.label_of(agent_name, query),
                                query)
            _running_status.set(status)             # this task's context: asyncio.to_thread hands it to the helper
            if self._on_start is not None:
                try:
                    self._on_start(agent_name, query, status)
                except Exception:                   # a display error must never stop the step
                    pass
            try:
                result = await run()
            finally:
                status.ended = time.monotonic()
            self._statuses[id(result)] = status
            return result


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
                 step_tokens: int = 0, closed: Optional[Callable[[], Any]] = None, record: Any = None) -> str:
    """Plan ``task``, run the helpers (read-only steps side by side, any other step alone) and return the result
    text. Progress goes to ``notify`` and questions to ``ask_user``, both on the calling thread. ``extra_models``
    adds the roles on other models (see ``build_helpers``); the planner stays on ``model``. ``step_tokens`` is each
    step's token share (see ``step_token_share``; 0 is none). Once ``closed()`` is true no further step starts.
    ``record`` (a ``TeamRecord``) writes the plan and each helper's whole report to disk as they happen.

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
        by_name = {tool.name: tool for tool in tools}
        pool = [tool for tool in tools if tool.name not in NOT_FOR_NEW_HELPERS]

        chat_label = model_label(model)
        models = {chat_label: model, **{extra.name: extra.model for extra in extra_models}}
        offered = [(chat_label, without_auto_prefix(chat_use)),
                   *((extra.name, without_auto_prefix(extra.use)) for extra in extra_models)]

        def make_helper(name: str, instructions: str, tool_names: List[str], model_name: str = "") -> AgentRunner:
            role = Role(name, instructions[:NEW_HELPER_DESCRIPTION_CHARS], tuple(tool_names),
                        f"The supervisor made you for this job: {instructions}")
            shown = f"{name}@{model_name}" if extra_models and model_name else name
            runner = _helper(role, models.get(model_name, model), by_name, addendum, describe_error, skills, shown,
                             every_mcp_tool=False)
            runner.job = job
            return runner

        # New helpers pick their tools from the same gated tools (no delete, move, team or delegate); max_depth=0
        # keeps the framework from giving anyone a delegate tool.
        spawning = SpawnConfig(enabled=True, tools=pool, capabilities=set(), max_spawns=MAX_NEW_HELPERS, max_depth=0)
        supervisor = TeamSupervisor(
            model=model, agents=helpers, max_subtasks=MAX_SUBTASKS, verbose=False, max_subtask_retries=0,
            max_plan_retries=1, ask_user=asker, max_questions=MAX_QUESTIONS, spawn_config=spawning,
            max_parallel=PARALLEL_HELPERS, closed=closed, make_helper=make_helper, models=offered,
            on_start=lambda agent, query, status: (
                record.started(status) if record is not None else None,
                tell("dispatch", {"agent": str(agent), "query": str(query), "label": status.label,
                                  "status": status})))
        result = None
        async with aclosing(supervisor.astream(task)) as events:
            async for event in events:
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "plan":
                    steps = [_plan_step(step, getattr(supervisor, "shown_step_name", None))
                             for step in (event.get("plan") or []) if isinstance(step, dict)]
                    if record is not None:
                        record.plan(steps)
                    tell("plan", {"steps": steps})
                elif kind == "subtask_result":
                    sub = event.get("result")
                    if sub is not None and getattr(sub, "agent", "") != "__spawn__":
                        failed = bool(getattr(sub, "skipped", False)) or _result_failed(sub)
                        if record is not None:
                            record.finished(supervisor.status_of(sub), sub, ok=not failed)
                        text = getattr(sub, "error", "") or getattr(sub, "content", "")
                        tell("result", {"agent": str(sub.agent), "ok": not failed, "text": _first_line(text),
                                        "partial": not failed and _ran_out_of_steps(sub),
                                        "skipped": bool(getattr(sub, "skipped", False)),
                                        "label": supervisor.label_of(sub.agent, sub.query),
                                        "status": supervisor.status_of(sub)})
                elif kind == "completion":
                    result = event.get("result")
        return result

    try:
        result = _run_on_loop(consume, job, pump)
    except Exception as error:                     # KeyboardInterrupt and SystemExit are not Exceptions
        return _recorded(record, _failure(error, describe_error))
    if result is None:
        return _recorded(record, f"Team job ended without a result. {MAY_HAVE_CHANGED}")
    return _recorded(record, format_result(result))


def _recorded(record: Any, text: str) -> str:
    """``text`` written to the job's record, with where the helpers' whole reports are for the main agent."""
    if record is None or not getattr(record, "folder", None):
        return text
    record.done(text)
    return (f"{text}\nEvery helper's whole report is saved in {record.shown_path}/ (job.md, then one file per "
            "step): read those files rather than redoing the work.")


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
