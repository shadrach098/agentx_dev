"""
Sub-agent creation for the Supervisor and for standalone runners.

Two ways to get a fresh, narrowly-scoped agent mid-task:

- **Planner-time**: a plan step carries a ``new_agent`` (name, free-form
  instructions, tools). The Supervisor builds it, runs the step on it, and
  discards it when the run ends.
- **Runtime**: a specialist calls the ``delegate`` tool to hand a piece of its
  own work to a fresh sub-agent and gets a short summary back.

Both go through one checkpoint, :class:`SpawnPolicy`, which clips every request to
the ceiling the developer set in :class:`SpawnConfig`. Instructions never widen the
ceiling.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from agentx_dev.Agents.Agent import AgentType
from agentx_dev.AsyncTools import AsyncStructuredTool
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.Runner.Persistence import Persistence, RunBudget, accepts_budget
from agentx_dev.Tools import StructuredTool, logger


# ----------------------------------------------------------------------------
# Configuration (moved here from Supervisor.py; still importable from there)
# ----------------------------------------------------------------------------


@dataclass
class SpawnRequest:
    """Planner-emitted request for a NEW specialist that doesn't exist
    in the current registry.

    Attributes:
        name: Short identifier the planner wants to use going forward.
        description: What the specialist should do — copied into the
            agent catalog for future planning turns.
        capabilities: List of capability keywords the planner needs.
            Recognized: 'web' (search + fetch), 'files' (read/write/edit
            inside sandbox), 'code' (Python execution), 'delete'
            (delete_files under sandbox). Unknown keywords are dropped
            with a note.
        rationale: Why the planner asked for this specialist. Shown to
            the approver so they can decide whether it's justified.
    """
    name: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class SpawnConfig:
    """How a Supervisor should handle dynamic-spawn requests.

    Attributes:
        enabled: Master switch. If False, the planner is not told about
            the spawn feature and can only use existing specialists.
        auto_spawn: When True, requests are approved silently. When
            False (default), each request goes through ``approver``
            (which defaults to a terminal ``input()`` prompt).
        approver: Optional callback ``(SpawnRequest) -> bool``. Return
            True to approve. Only called when auto_spawn=False.
        allowed_paths: File-system sandbox for spawned specialists that
            request ``files`` / ``code`` / ``delete`` capabilities.
            Defaults to ["./workspace"].
        auto_spawn_allowed_caps: SECURITY GATE for auto_spawn=True.
            When set (e.g. {"web"}), a planner-emitted spawn is
            AUTO-approved only if EVERY requested capability is in this
            set — anything else falls through to ``approver`` (or is
            refused if none is set). ``None`` means "no restriction, any
            cap the planner asks for is auto-granted" — the historical
            behaviour, kept for backward compat but explicitly opt-out.
            Rationale: the planner's JSON is downstream of user text, so
            a prompt-injected task could ask for ``capabilities:["code"]``
            or ``["delete"]`` and get silent RCE / file destruction under
            auto_spawn. Recommended defaults: ``{"web"}`` for research
            agents, ``set()`` (empty) to disable auto-spawn entirely
            without unsetting the flag, ``None`` only when you trust the
            planner's source.
        tools: (3.6) Pool of tool objects a spawned agent may pick from by
            name. Setting this (or ``capabilities``) switches to ceiling
            mode: spawns inside the ceiling need no approval.
        capabilities: (3.6) Preset words a spawn may use: 'web', 'files',
            'files_read' (read-only), 'code', 'delete'. A ceiling, not a
            request: anything outside it is dropped.
        max_depth: (3.6) 1 (default) = a spawned agent cannot spawn more;
            specialists you registered can delegate. Raise to allow deeper trees.
        max_spawns: Defaults to 3 in legacy mode and 6 in ceiling mode;
            counts planner-time spawns and ``delegate`` calls together.
    """
    enabled: bool = False
    auto_spawn: bool = False
    approver: Optional[Callable[[SpawnRequest], bool]] = None
    allowed_paths: List[str] = field(default_factory=lambda: ["./workspace"])
    max_spawns: Optional[int] = None
    auto_spawn_allowed_caps: Optional[Set[str]] = None
    tools: Optional[List[Any]] = None
    capabilities: Optional[Set[str]] = None
    max_depth: int = 1

    @property
    def ceiling_mode(self) -> bool:
        """True when ``tools`` or ``capabilities`` is set: spawns inside that ceiling
        need no approval. Otherwise the legacy approval flow applies."""
        return self.tools is not None or self.capabilities is not None

    @property
    def effective_max_spawns(self) -> int:
        """``max_spawns`` if set, else 6 in ceiling mode and 3 in legacy mode."""
        if self.max_spawns is not None:
            return int(self.max_spawns)
        return 6 if self.ceiling_mode else 3


def _default_interactive_approver(request: SpawnRequest) -> bool:
    """Terminal-based approver used when SpawnConfig.approver is None.
    Returns False if there's no TTY (e.g. running headless) so a
    supervisor without an explicit callback won't silently spawn."""
    if not sys.stdin.isatty():
        print(
            f"[supervisor.spawn] REQUEST '{request.name}' — no TTY, refusing "
            f"(set SpawnConfig.approver=<callable> or auto_spawn=True)"
        )
        return False
    print(
        f"\n[supervisor.spawn] The planner wants to create a new specialist.\n"
        f"  name         : {request.name}\n"
        f"  description  : {request.description}\n"
        f"  capabilities : {', '.join(request.capabilities) or '(none)'}\n"
        f"  rationale    : {request.rationale or '(none)'}"
    )
    answer = input("Approve? [y/N] ").strip().lower()
    return answer in ("y", "yes")


_SPAWNED_SPECIALIST_ADDENDUM = """You were spawned by a Supervisor to handle a specific sub-task. The Supervisor will synthesize your reply into the user's final answer. To make that possible:

- INCLUDE THE ACTUAL DATA IN YOUR FINAL ANSWER. Do not just report a status like "the file was written" or "task complete". If you extracted a title, list it. If you found 3 competitors, name them + positioning + URL in your reply. If you saved a report, include a concise summary of its contents. The Supervisor cannot read your files — it can only read your reply.
- Do NOT invent data. If a fetch failed or a page didn't contain the field asked for, say so plainly ("no phone numbers were found on the page"). Say it explicitly rather than guess.
- Keep the reply structured (bullet lists, tables, key: value lines) so the Supervisor's synthesis step can lift verbatim facts out.
- For any STRUCTURAL CODE METRIC — class counts, method counts per class, function names, duplicate-function detection, cyclomatic complexity, call-graph analysis — USE the `ast` module inside run_python. Parse the file with `ast.parse(source)` and walk `ast.ClassDef` / `ast.FunctionDef` / `ast.AsyncFunctionDef` nodes. Do NOT use regex or `line.startswith('def ')` for these — that approach misses nested defs, counts strings-that-happen-to-contain-'class' as classes, treats keywords like `for`/`while` inside a function body as CC contributors for the wrong function, and produces obviously-wrong numbers (functions with CC=400, "function names" that are actually Python keywords). If you find yourself computing a per-function metric via string heuristics, stop and rewrite using ast."""


# ----------------------------------------------------------------------------
# Specs
# ----------------------------------------------------------------------------

MAX_INSTRUCTIONS_CHARS = 4000
SUMMARY_CAP_CHARS = 4000
DELEGATE_TOOL_NAME = "delegate"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RESERVED_DELEGATE_RE = re.compile(r"^delegate_\d+$")       # the names delegate calls get

# Preset capability word -> the concrete tool names it installs.
PRESET_TOOLS: Dict[str, Set[str]] = {
    "web": {"web_search", "web_fetch"},
    "files": {"read_path", "list_directory", "find_files", "grep", "write_file", "edit_file"},
    "files_read": {"read_path", "list_directory", "find_files", "grep"},
    "code": {"run_python"},
    "delete": {"delete_path"},
}

# A single tool name resolves to the narrowest preset that contains it and that the ceiling allows.
TOOL_TO_PRESETS: Dict[str, Tuple[str, ...]] = {
    "web_search": ("web",), "web_fetch": ("web",),
    "read_path": ("files_read", "files"), "list_directory": ("files_read", "files"),
    "find_files": ("files_read", "files"), "grep": ("files_read", "files"),
    "write_file": ("files",), "edit_file": ("files",),
    "run_python": ("code",), "delete_path": ("delete",),
}

_PRESET_PERMS: Dict[str, Dict[str, bool]] = {
    "files": dict(read_files=True, write_files=True, edit_files=True, list_directories=True),
    "files_read": dict(read_files=True, list_directories=True),
    "code": dict(execute_python=True),
    "delete": dict(delete_files=True),
}

_PRESET_DOC: Dict[str, str] = {
    "web": "web_search + web_fetch (search the web, fetch pages)",
    "files": "read, write, edit and list files inside the sandbox",
    "files_read": "read, list, find and grep files inside the sandbox (read-only)",
    "code": "run_python (execute Python for computation)",
    "delete": "delete files inside the sandbox",
}

_DEFAULT_DELEGATE_INSTRUCTIONS = (
    "You are a focused helper. Do exactly the task you are given and report the result."
)


class SpecError(ValueError):
    """A ``new_agent`` / legacy spawn description is malformed."""


@dataclass(frozen=True)
class AgentSpec:
    """One sub-agent to build: who it is and which tools it asks for."""

    name: str
    instructions: str
    tools: Tuple[str, ...] = ()
    origin: str = "plan"            # "plan" | "delegate"


def parse_agent_spec(raw: Any, origin: str = "plan", *, reserve_dunder: bool = True) -> AgentSpec:
    """Validate and normalize a ``new_agent`` object. Raises :class:`SpecError`.

    Names starting with ``__`` (``__spawn__`` is plan syntax) and names of the form
    ``delegate_<N>`` (generated for ``delegate`` calls) are reserved. ``reserve_dunder=False``
    checks only the ``delegate_<N>`` form (used for legacy ``__spawn__`` steps)."""
    if not isinstance(raw, dict):
        raise SpecError("new_agent must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise SpecError("name must match [A-Za-z0-9_-] and be 1-40 characters")
    if (reserve_dunder and name.strip().startswith("__")) or _RESERVED_DELEGATE_RE.match(name.strip()):
        raise SpecError("name is reserved")
    instructions = raw.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise SpecError("instructions must be a non-empty string")
    tools = raw.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SpecError("tools must be a list of tool names")
    cleaned = tuple(dict.fromkeys(t.strip() for t in tools if t.strip()))
    return AgentSpec(
        name=name.strip(),
        instructions=instructions.strip()[:MAX_INSTRUCTIONS_CHARS],
        tools=cleaned,
        origin=origin,
    )


def spec_from_legacy_spawn(step: Dict[str, Any]) -> AgentSpec:
    """Turn a 3.5 ``{"agent": "__spawn__", name, description, capabilities}`` step into a spec."""
    return parse_agent_spec({
        "name": str(step.get("name", "")).strip(),
        "instructions": str(step.get("description", "")).strip(),
        "tools": [str(c).strip() for c in (step.get("capabilities") or []) if c],
    }, reserve_dunder=False)


class SpawnRefused(Exception):
    """The policy declined to build a sub-agent. ``reason`` is shown to the planner / caller."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DelegationFailed(Exception):
    """Raised by the ``delegate`` tool when the sub-agent did not finish. The tool registry
    turns it into a tool error, so the caller's stuck tracker sees it."""


@dataclass
class Built:
    """What the policy hands back for one spec."""

    spec: AgentSpec
    runner: Any                      # None when the name resolved to a developer-registered specialist
    description: str
    granted: List[str]               # preset words and pool tool names actually granted
    dropped: List[str]               # requested names that were clipped
    reused: Optional[str] = None     # None | "registered" | "spawned"


class SpawnRun:
    """Per-run state shared by every spawn in one Supervisor run (or one runner invocation)."""

    def __init__(self) -> None:
        self.count = 0
        self.built: Dict[str, Built] = {}
        self.records: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._delegates = 0

    def next_delegate_name(self) -> str:
        self._delegates += 1
        return f"delegate_{self._delegates}"

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def drain(self) -> List[Dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def finish(self, name: str, outcome: str, chars: int) -> None:
        """Fill in the outcome of the most recent open record for ``name``."""
        for rec in reversed(self.records):
            if rec["name"] == name and rec.get("outcome") is None:
                rec["outcome"] = outcome
                rec["chars"] = chars
                return


# ----------------------------------------------------------------------------
# The policy: the single checkpoint
# ----------------------------------------------------------------------------

_SPAWN_INSTRUCTION = """

── CREATING NEW SPECIALISTS ─────────────────────────────────────────
You may define a NEW specialist inline when none of the available specialists fits a sub-task: a different role, different instructions, or tools nobody else has. Use it sparingly: at most <<MAX>> new specialists per run.

Put the definition on the step that first uses it:

  {"id": "s2", "query": "<the sub-task>",
   "new_agent": {"name": "<short_snake_case>",
                 "instructions": "<who the specialist is and exactly what data it must return>",
                 "tools": ["<tool name>", "..."]}}

Later steps reuse it by name: {"id": "s3", "agent": "<that name>", "query": "..."}. Do NOT define it again.

Rules:
  - Prefer an existing specialist whenever one fits. Define a new one only for a genuinely different role or toolset.
  - "instructions" must say what the specialist should RETURN (facts, numbers, a table), not just what to do. The framework adds: return the real data, never invent it.
  - "tools" may only name tools from the menu below; anything else is dropped. A specialist with no tools can still reason.
  - A step uses EITHER "agent" OR "new_agent", never both. "name" is letters, digits, "_" or "-" (max 40).

Tools you can grant:
<<MENU>>
────────────────────────────────────────────────────────────────────
"""


def spawn_instruction(policy: "SpawnPolicy") -> str:
    """The planner-prompt block that teaches ``new_agent``."""
    return (_SPAWN_INSTRUCTION
            .replace("<<MAX>>", str(policy.config.effective_max_spawns))
            .replace("<<MENU>>", policy.menu()))


class SpawnPolicy:
    """Turns an :class:`AgentSpec` into a runner, inside the ceiling in ``config``.

    One policy serves one Supervisor run (or one invocation of a runner built with
    ``delegation=``). Everything that creates a sub-agent calls :meth:`build` /
    :meth:`obtain` here, so the rules cannot drift apart.
    """

    def __init__(
        self,
        config: SpawnConfig,
        model: Any,
        *,
        persistence: Optional[Persistence] = None,
        is_async: bool = False,
        verbose: bool = False,
        run: Optional[SpawnRun] = None,
        budget: Optional[RunBudget] = None,
    ):
        self.config = config
        self.model = model
        self.persistence = persistence
        self.is_async = is_async
        self.verbose = verbose
        self.run = run or SpawnRun()
        # The supervisor run's RunBudget (set by Supervisor.stream / AsyncSupervisor.astream).
        # ``delegate`` falls back to it when the calling runner's own _active_budget is gone,
        # e.g. a parallel step on the same specialist finished first and cleared it.
        self.budget = budget

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def new_run(self) -> None:
        self.run = SpawnRun()
        self.budget = None

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"[spawn] {text}")

    # -- the ceiling ----------------------------------------------------------

    def _preset_allowed(self, preset: str) -> bool:
        cfg = self.config
        if not cfg.ceiling_mode:
            return True
        allowed = {c.lower() for c in (cfg.capabilities or set())}
        return preset in allowed or (preset == "files_read" and "files" in allowed)

    def menu(self) -> str:
        """The tools a planner may name, one per line, for the planner prompt."""
        lines: List[str] = []
        for preset in ("web", "files_read", "files", "code", "delete"):
            if self._preset_allowed(preset):
                lines.append(f"  - {preset}: {_PRESET_DOC[preset]}")
        for tool in self.config.tools or []:
            first = str(getattr(tool, "description", "") or "").strip().splitlines()
            lines.append(f"  - {tool.name}: {first[0][:120] if first else ''}".rstrip())
        return "\n".join(lines) if lines else "  (none: new specialists can only reason)"

    def resolve(self, requested: Iterable[str]) -> Tuple[List[str], List[Any], List[str]]:
        """Clip ``requested`` to the ceiling. Returns ``(granted presets, pool tools, dropped names)``."""
        pool = {t.name: t for t in (self.config.tools or []) if getattr(t, "name", None)}
        granted: List[str] = []
        tools: List[Any] = []
        dropped: List[str] = []
        for raw in requested:
            word = str(raw).strip()
            low = word.lower()
            if low in PRESET_TOOLS:
                if self._preset_allowed(low):
                    if low not in granted:
                        granted.append(low)
                else:
                    dropped.append(word)
            elif word in pool:
                if pool[word] not in tools:
                    tools.append(pool[word])
            elif word in TOOL_TO_PRESETS:
                pick = next((p for p in TOOL_TO_PRESETS[word] if self._preset_allowed(p)), None)
                if pick is None:
                    dropped.append(word)
                elif pick not in granted:
                    granted.append(pick)
            else:
                dropped.append(word)
        if "files" in granted and "files_read" in granted:
            granted.remove("files_read")            # "files" already covers read access
        return granted, tools, dropped

    # -- approval -------------------------------------------------------------

    def _approve(self, spec: AgentSpec) -> bool:
        cfg = self.config
        req = SpawnRequest(name=spec.name, description=spec.instructions,
                           capabilities=list(spec.tools), rationale="")
        try:
            if cfg.ceiling_mode:
                return bool(cfg.approver(req)) if cfg.approver is not None else True
            if cfg.auto_spawn:
                asked = {c.lower() for c in req.capabilities}
                allow = cfg.auto_spawn_allowed_caps
                if allow is not None and not asked.issubset(allow):
                    if cfg.approver is None:
                        return False
                    return bool(cfg.approver(req))
                return True
            return bool((cfg.approver or _default_interactive_approver)(req))
        except Exception as e:                       # an approver that raises is a "no"
            logger.warning(f"spawn approver raised; treating as a refusal: {e}")
            return False

    # -- building -------------------------------------------------------------

    def _describe(self, spec: AgentSpec, granted: List[str]) -> str:
        first = spec.instructions.strip().splitlines()[0][:200]
        return f"{first} (tools: {', '.join(granted) or 'none'})"

    def _make_runner(self, spec: AgentSpec, granted_presets: List[str], pool_tools: List[Any],
                     is_async: bool) -> Any:
        from agentx_dev.DefaultTools import Permissions
        from agentx_dev.WebTools import web_fetch_tool, web_search_tool

        cfg = self.config
        flags: Dict[str, bool] = {}
        tools: List[Any] = []
        file_caps = {"files", "code", "delete"} & set(granted_presets)
        cache_dir = str(cfg.allowed_paths[0]) if (cfg.allowed_paths and file_caps) else None
        for preset in granted_presets:
            if preset == "web":
                tools.extend([web_search_tool(), web_fetch_tool(cache_dir=cache_dir)])
            else:
                flags.update(_PRESET_PERMS[preset])
        tools.extend(pool_tools)
        perms = Permissions(allowed_paths=list(cfg.allowed_paths), **flags) if flags else None
        cls = AsyncAgentRunner if is_async else AgentRunner
        return cls(
            model=self.model,
            agent=AgentType.ReAct,
            tools=tools,
            permissions=perms,
            max_iterations=15,
            verbose=False,
            system_addendum=_SPAWNED_SPECIALIST_ADDENDUM + "\n\nYour role:\n" + spec.instructions,
        )

    def build(self, spec: AgentSpec, depth: int = 1, *, is_async: Optional[bool] = None) -> Built:
        """Build a runner for ``spec`` at ``depth`` or raise :class:`SpawnRefused`.
        Emits the ``spawn`` event either way."""
        try:
            built = self._build(spec, depth, self.is_async if is_async is None else is_async)
        except SpawnRefused as e:
            self.run.emit({"type": "spawn", "name": spec.name, "origin": spec.origin,
                           "tools": [], "dropped": list(spec.tools), "reused": None,
                           "refused": e.reason, "capabilities": [], "rerouted_from": None})
            self._say(f"refused '{spec.name}': {e.reason}")
            raise
        self.run.count += 1
        self.run.built[spec.name] = built
        self.run.records.append({"name": spec.name, "origin": spec.origin,
                                 "tools": list(built.granted), "dropped": list(built.dropped),
                                 "outcome": None, "chars": None})
        self._announce(built)
        return built

    def _build(self, spec: AgentSpec, depth: int, is_async: bool) -> Built:
        cfg = self.config
        if not cfg.enabled:
            raise SpawnRefused("spawning is disabled")
        if self.run.count >= cfg.effective_max_spawns:
            raise SpawnRefused("spawn limit reached")
        presets, pool_tools, dropped = self.resolve(spec.tools)
        if spec.tools and not presets and not pool_tools:
            raise SpawnRefused("no usable tools")
        if not self._approve(spec):
            raise SpawnRefused("not approved")
        try:
            runner = self._make_runner(spec, presets, pool_tools, is_async)
        except Exception as e:                       # e.g. a pool tool clashing with a default tool
            raise SpawnRefused(f"could not build the agent: {e}") from e
        if self.persistence is not None:
            runner.persistence = self.persistence
        if depth < cfg.max_depth:
            try:
                runner.add_tool(make_delegate_tool(runner, self, depth + 1))
            except Exception as e:                   # e.g. a pool tool already named "delegate"
                raise SpawnRefused(f"could not build the agent: {e}") from e
        granted = presets + [t.name for t in pool_tools]
        return Built(spec=spec, runner=runner, description=self._describe(spec, granted),
                     granted=granted, dropped=dropped)

    def obtain(self, spec: AgentSpec, depth: int = 1, *, registered: Iterable[str] = (),
               is_async: Optional[bool] = None) -> Built:
        """Reuse or build. A name the developer registered is not rebuilt; an identical
        earlier spawn is reused for free; a clashing name gets a numeric suffix."""
        taken = set(registered)
        prior = self.run.built.get(spec.name)
        if prior is None and spec.name in taken:
            built = Built(spec=spec, runner=None, description="", granted=[], dropped=[], reused="registered")
            self._announce(built)
            return built
        if prior is not None:
            if (prior.spec.instructions, prior.spec.tools) == (spec.instructions, spec.tools):
                built = dataclasses.replace(prior, reused="spawned")
                self._announce(built)
                return built
            n = 2
            while f"{spec.name}_{n}" in taken or f"{spec.name}_{n}" in self.run.built:
                n += 1
            spec = dataclasses.replace(spec, name=f"{spec.name}_{n}")
        return self.build(spec, depth, is_async=is_async)

    def _announce(self, built: Built) -> None:
        self.run.emit({"type": "spawn", "name": built.spec.name, "origin": built.spec.origin,
                       "tools": list(built.granted), "dropped": list(built.dropped),
                       "reused": built.reused, "refused": None,
                       "capabilities": list(built.granted), "rerouted_from": None})
        what = f"reusing {built.reused} agent" if built.reused else "built"
        self._say(f"{what} '{built.spec.name}' ({built.spec.origin}) tools={built.granted or 'none'}"
                  + (f" dropped={built.dropped}" if built.dropped else ""))


# ----------------------------------------------------------------------------
# The delegate tool
# ----------------------------------------------------------------------------

class DelegateArgs(BaseModel):
    task: str = Field(description="Everything the sub-agent needs to do the job. It sees ONLY this, "
                                  "not your conversation, so include every fact it needs.")
    instructions: str = Field(default="", description="Who the sub-agent is and exactly what it must return.")
    tools: List[str] = Field(default_factory=list,
                             description="Names of the tools it may use. Anything not offered is dropped.")


DELEGATE_DESCRIPTION = (
    "Hand a self-contained piece of your work to a fresh sub-agent and get its final answer back. "
    "The sub-agent starts with a clean context and sees only `task`, so put every fact it needs in "
    "`task`. Use it for large side jobs that would clutter your own context (reading many files, "
    "researching a subtopic), not for trivial steps. It returns a short summary."
)


def cap_summary(text: Any, cap: int = SUMMARY_CAP_CHARS) -> str:
    """The sub-agent's answer as the caller sees it: at most ``cap`` characters plus a marker."""
    s = str(text)
    return s if len(s) <= cap else s[:cap] + "\n[truncated]"


def _is_async_runner(runner: Any) -> bool:
    return asyncio.iscoroutinefunction(getattr(runner, "Initialize", None))


def _delegate_spec(policy: SpawnPolicy, instructions: Any, tools: Any) -> AgentSpec:
    inst = str(instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS] or _DEFAULT_DELEGATE_INSTRUCTIONS
    names = tuple(dict.fromkeys(str(t).strip() for t in (tools or []) if str(t).strip()))
    return AgentSpec(name=policy.run.next_delegate_name(), instructions=inst, tools=names, origin="delegate")


def _prepare(policy: SpawnPolicy, depth: int, is_async: bool, task: Any, instructions: Any,
             tools: Any) -> Tuple[Optional[str], Optional[Built]]:
    """``(refusal text, None)`` or ``(None, built sub-agent)``."""
    if not str(task or "").strip():
        return "delegation refused: task is empty; do this yourself", None
    spec = _delegate_spec(policy, instructions, tools)
    try:
        return None, policy.build(spec, depth, is_async=is_async)
    except SpawnRefused as e:
        return f"delegation refused: {e.reason}; do this yourself", None


def _delegate_error(policy: SpawnPolicy, built: Built, exc: BaseException) -> DelegationFailed:
    name = built.spec.name
    policy.run.finish(name, "error", 0)
    policy.run.emit({"type": "delegate_result", "name": name, "outcome": "error", "chars": 0})
    return DelegationFailed(f"[delegate failed: error] {exc}")


def _delegate_result(policy: SpawnPolicy, built: Built, completion: Any) -> str:
    name = built.spec.name
    outcome = getattr(completion, "outcome", "done")
    outcome = outcome if isinstance(outcome, str) else "done"
    text = str(getattr(completion, "content", "") or "")
    policy.run.finish(name, outcome, len(text))
    policy.run.emit({"type": "delegate_result", "name": name, "outcome": outcome, "chars": len(text)})
    if outcome != "done":
        raise DelegationFailed(f"[delegate failed: {outcome}] {cap_summary(text)}")
    note = (f"\n[note: these tools were not granted: {', '.join(built.dropped)}]" if built.dropped else "")
    return cap_summary(text) + note


def make_delegate_tool(parent: Any, policy: SpawnPolicy, depth: int) -> Any:
    """The ``delegate`` tool for ``parent``. ``depth`` is the depth a sub-agent built by it gets.
    An async parent gets an async tool that builds async sub-agents; a sync parent a sync tool."""
    description = DELEGATE_DESCRIPTION + "\nTools you may grant:\n" + policy.menu()

    if _is_async_runner(parent):
        async def delegate(task: str, instructions: str = "", tools: Optional[List[str]] = None) -> str:
            refusal, built = _prepare(policy, depth, True, task, instructions, tools)
            if refusal is not None:
                return refusal
            budget = getattr(parent, "_active_budget", None) or policy.budget
            kw = {"_budget": budget} if budget is not None and accepts_budget(built.runner.Initialize) else {}
            try:
                completion = await built.runner.Initialize(task, **kw)
            except Exception as e:
                raise _delegate_error(policy, built, e) from e
            return _delegate_result(policy, built, completion)

        tool = AsyncStructuredTool(func=delegate, args_schema=DelegateArgs,
                                   name=DELEGATE_TOOL_NAME, description=description)
        tool.cacheable = False        # a delegation has side effects: never answer it from the cache
        return tool

    def delegate(task: str, instructions: str = "", tools: Optional[List[str]] = None) -> str:
        refusal, built = _prepare(policy, depth, False, task, instructions, tools)
        if refusal is not None:
            return refusal
        budget = getattr(parent, "_active_budget", None) or policy.budget
        kw = {"_budget": budget} if budget is not None and accepts_budget(built.runner.Initialize) else {}
        try:
            completion = built.runner.Initialize(task, **kw)
        except Exception as e:
            raise _delegate_error(policy, built, e) from e
        return _delegate_result(policy, built, completion)

    tool = StructuredTool(func=delegate, args_schema=DelegateArgs,
                          name=DELEGATE_TOOL_NAME, description=description)
    tool.cacheable = False            # a delegation has side effects: never answer it from the cache
    return tool


@contextmanager
def attach_delegation(runners: Iterable[Any], policy: SpawnPolicy, depth: int = 0):
    """Give every runner in ``runners`` the ``delegate`` tool for the duration of the block.

    Nothing is attached unless the config is in ceiling mode (``capabilities=`` or ``tools=``
    set): a legacy 3.5 config keeps its approval flow for planner spawns and gives specialists
    no ``delegate``. Runners that cannot take a tool (no ``add_tool``), that already have a
    ``delegate`` tool, or are at ``depth >= max_depth`` are left alone. The tool is removed on
    exit, even if the block raises, so the developer's runners are exactly as they were."""
    attached: List[Any] = []
    if policy.enabled and policy.config.ceiling_mode and depth < policy.config.max_depth:
        for r in runners:
            if not callable(getattr(r, "add_tool", None)) or not hasattr(r, "registry"):
                continue
            if r.registry.has(DELEGATE_TOOL_NAME):
                continue
            r.add_tool(make_delegate_tool(r, policy, depth + 1))
            attached.append(r)
    try:
        yield
    finally:
        for r in attached:
            r.remove_tool(DELEGATE_TOOL_NAME)
