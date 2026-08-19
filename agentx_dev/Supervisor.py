"""
Supervisor / multi-agent orchestration layer for AgentX.

Provides ``Supervisor`` and ``AsyncSupervisor`` classes that:
1. Accept a registry of named specialist sub-agents.
2. Use an LLM to decompose a high-level task into sub-tasks and assign each to
   the best specialist.
3. Execute those sub-tasks (sequentially for the sync supervisor, concurrently
   for the async supervisor).
4. Synthesize the sub-agent results into a single final answer.

Also supports DYNAMIC SPAWNING — if enabled, the planner can request a
new specialist mid-plan. In auto_spawn mode the framework creates the
requested specialist and adds it to the registry silently; otherwise a
callback (defaulting to interactive terminal input) approves or rejects
the request per-spawn.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple, Union, Any

from pydantic import BaseModel, Field

from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.Tools import logger


# ----------------------------------------------------------------------------
# Dynamic sub-agent spawning
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
        max_spawns: Upper bound on how many new specialists can be
            added during one Supervisor run (guards against runaway).
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
    """
    enabled: bool = False
    auto_spawn: bool = False
    approver: Optional[Callable[[SpawnRequest], bool]] = None
    allowed_paths: List[str] = field(default_factory=lambda: ["./workspace"])
    max_spawns: int = 3
    auto_spawn_allowed_caps: Optional[Set[str]] = None


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


def _build_spawned_agent(
    request: SpawnRequest,
    model: BaseChatModel,
    allowed_paths: List[str],
) -> Tuple[str, AgentRunner]:
    """Turn a SpawnRequest into a (description, AgentRunner) pair ready
    to register with the Supervisor.

    The permission set is derived conservatively from ``capabilities``:
    only what was explicitly requested gets granted, and file ops are
    always sandboxed to ``allowed_paths``. Unknown capability keywords
    are ignored (with a returned note in the description).

    The spawned runner always carries the "include findings verbatim"
    system addendum so the Supervisor's synthesis step has real data
    to synthesize from (a specialist that returns "task complete"
    leaves the synthesizer with nothing but its imagination).
    """
    from agentx_dev.DefaultTools import Permissions
    from agentx_dev.WebTools import web_fetch_tool, web_search_tool

    caps = {c.lower() for c in request.capabilities}
    perms_kwargs: Dict[str, Any] = {"allowed_paths": list(allowed_paths)}
    tools: List[Any] = []
    granted: List[str] = []
    unknown: List[str] = []

    # Pick a cache directory for web_fetch: the first allowed path,
    # unless the spec doesn't have any file-side capability at all
    # (no "files"/"code"/"delete") — then caching would be dead disk
    # writes the agent can't read anyway.
    file_caps = {"files", "code", "delete"} & caps
    cache_dir = str(allowed_paths[0]) if (allowed_paths and file_caps) else None

    for cap in caps:
        if cap == "web":
            tools.extend([web_search_tool(), web_fetch_tool(cache_dir=cache_dir)])
            granted.append(
                "web (search+fetch"
                + (f", cached to {cache_dir}" if cache_dir else "")
                + ")"
            )
        elif cap == "files":
            perms_kwargs.update(
                read_files=True, write_files=True, edit_files=True,
                list_directories=True,
            )
            granted.append("files (read/write/edit/list)")
        elif cap == "delete":
            perms_kwargs["delete_files"] = True
            granted.append("delete")
        elif cap == "code":
            perms_kwargs["execute_python"] = True
            granted.append("code (run_python)")
        else:
            unknown.append(cap)

    perms = Permissions(**perms_kwargs)

    runner = AgentRunner(
        model=model,
        agent=AgentType.ReAct,
        tools=tools,
        permissions=perms if any(v for k, v in perms_kwargs.items()
                                 if k not in ("allowed_paths",)) else None,
        max_iterations=15,
        use_function_calling=True,
        verbose=False,
        system_addendum=_SPAWNED_SPECIALIST_ADDENDUM,
    )
    note = f" (granted: {', '.join(granted) or 'none'}"
    if unknown:
        note += f"; unknown skipped: {', '.join(unknown)}"
    note += ")"
    description = request.description + note
    return description, runner


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------

class SubtaskResult(BaseModel):
    """Result from a single specialist sub-agent invocation.

    ``content`` is the human-readable answer text (always present).
    ``output`` carries the specialist's validated Pydantic instance when
    the underlying runner declared an ``output_schema`` — machine-readable
    structured data that downstream steps can consume without re-parsing
    prose. Stays ``None`` for schema-less specialists, so existing
    consumers of ``content`` are unaffected.

    3.3 additions (all optional; absent in legacy plans):
    ``step_id`` is the plan step's id, ``depends_on`` the ids it consumed,
    and ``skipped`` marks steps that never dispatched — either because a
    dependency failed (``error`` explains the cascade) or because a
    ``skip_when`` condition matched (``error`` is None; the reason is in
    ``content``).
    """

    agent: str
    query: str
    content: str
    error: Optional[str] = None
    output: Optional[Any] = None
    step_id: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    skipped: bool = False

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class Specialist:
    """Registry entry for one specialist (3.3).

    ``agents={}`` accepts either the classic ``(description, runner)``
    tuple or a ``Specialist``. Tuples are wrapped internally, so nothing
    breaks; ``Specialist`` adds planner-facing metadata:

    - ``depends_on``: names of specialists this one TYPICALLY follows.
      A hint rendered into the planning catalog — never an execution
      constraint (the same specialist can appear twice in one plan, so
      name-level deps are ambiguous at runtime; step-ids are not).
    - ``output_schema``: shown in the catalog so the planner can write
      ``skip_when`` conditions against real field names. Defaults from
      ``runner.output_schema`` when unset.
    - ``when_to_use``: extra routing guidance for the planner.

    Iterating a Specialist yields ``(description, runner)`` so existing
    tuple-unpacking call sites keep working unchanged.
    """

    description: str
    runner: Any
    depends_on: List[str] = field(default_factory=list)
    output_schema: Optional[type] = None
    when_to_use: str = ""

    def __post_init__(self):
        if self.output_schema is None:
            self.output_schema = getattr(self.runner, "output_schema", None)

    def __iter__(self):
        # Tuple-compat: `desc, runner = specialist` and
        # `for name, (desc, _) in agents.items()` both keep working.
        return iter((self.description, self.runner))


def _normalize_agents(agents: Dict[str, Any]) -> Dict[str, Specialist]:
    """Wrap classic ``(description, runner)`` tuples as ``Specialist``.
    Existing Specialist values pass through untouched."""
    out: Dict[str, Specialist] = {}
    for name, entry in (agents or {}).items():
        if isinstance(entry, Specialist):
            out[name] = entry
        else:
            desc, runner = entry
            out[name] = Specialist(description=desc, runner=runner)
    return out


class SupervisorResult(BaseModel):
    """Aggregate result produced by a Supervisor run."""

    query: str
    content: str  # final synthesized answer
    subtasks: List[SubtaskResult] = Field(default_factory=list)
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

SUPERVISOR_SPAWN_INSTRUCTION = """

── DYNAMIC SPECIALIST SPAWNING ──────────────────────────────────────
You can SPAWN a new specialist mid-plan when the existing catalog can't cover a capability the task needs. This is not a fallback — it's the CORRECT move whenever the task requires a capability no registered agent has.

## WHEN TO SPAWN (mandatory triggers — spawn BEFORE dispatching such a step)

Before assigning any step to an existing specialist, check the specialist's description against the sub-task's needs. Spawn if ANY of these apply:

  - The sub-task requires COMPUTATION over data (LOC counts, statistics, top-N ranking, aggregation, deduplication, cyclomatic complexity, AST parsing, JSON/CSV transformation, math over many values) AND no registered specialist has 'code' capability. Grep+read+list cannot count / sort / aggregate — you need `run_python`. SPAWN a code specialist.
  - The sub-task requires WEB access (search, fetch a URL) AND no registered specialist has 'web' capability. SPAWN with 'web'.
  - The sub-task requires WRITING or EDITING files AND no registered specialist has 'files' capability. SPAWN with 'files'.
  - The sub-task requires DELETING files AND no registered specialist has 'delete'. SPAWN with 'delete'.

If you assign a code-computation step to a specialist that only has grep/find/read, that step WILL FAIL. The specialist will try to call `run_python`, get "tool not found", spiral, and the framework will hard-abort with no data. Spawn instead.

## DO NOT SPAWN DUPLICATES (the framework will REFUSE and your plan will fail)

Before emitting ANY spawn, scan the "Available specialist agents" catalog above and ask:
  1. Does an existing specialist's description already mention the capability I want (web / files / code / delete)?
  2. Could an existing specialist's tool set cover this — even if its NAME sounds different from what I want to spawn?

If yes to either — DISPATCH to that existing specialist instead. Do NOT spawn a new one. Every spawn with capabilities that overlap with an existing specialist WILL BE REFUSED by the framework's capability-overlap guard: the framework prints "REFUSED — existing specialist 'X' already covers caps ..." and the __spawn__ step's follow-up dispatch step then fails because your named specialist was never registered.

Examples of INVALID duplicate spawns:
  - Catalog has "researcher: web_search + web_fetch". You emit __spawn__ for "web_researcher" with caps=['web']. REFUSED. Dispatch to "researcher" instead.
  - Catalog has "analyst: run_python + files". You emit __spawn__ for "stats_analyst" with caps=['code']. REFUSED. Reuse "analyst".
  - Task has multiple web-lookup sub-tasks. Spawn ONE web specialist (or reuse the registered one), then dispatch to it N times. Do NOT spawn once per sub-task.

If you genuinely need a specialist with a NARROWER role but overlapping tools (say, "focused only on Barrie" vs. a general researcher) — you still can't spawn it because tools are the same. Instead, dispatch to the existing specialist with a more specific query.

## HOW TO SPAWN

A spawn step goes in the plan array like a regular step, but with agent="__spawn__" plus additional fields:

  {{"agent": "__spawn__",
    "name": "<short lowercase identifier — used in later steps>",
    "description": "<what this specialist should do>",
    "capabilities": ["web", "files", "code", "delete"],   # any subset
    "rationale": "<one sentence: why the existing specialists don't fit>"}}

Then LATER steps that need that specialist reference it by the name you gave:
  {{"agent": "<the name>", "query": "..."}}

Recognized capabilities (grant only what's needed):
  - web    : web_search + web_fetch tools
  - files  : read/write/edit/list inside sandbox
  - code   : run_python (execute Python for computation, AST, stats, aggregation)
  - delete : delete_files inside sandbox

## WORKED EXAMPLES

### Example A — task asks for counts, stats, and rankings; catalog has no code specialist
Registered agents: explorer (read/grep/find only), reporter (write only).
Task: "Count Python files, total LOC, top 5 files by line count, class/def counts, TODOs, dependencies. Save a markdown report."

CORRECT plan:
  1. {{"agent": "__spawn__", "name": "analyst", "description": "Python code execution for computation over the source tree", "capabilities": ["code", "files"], "rationale": "None of the registered agents can run Python; the task needs LOC counting, ranking, and aggregation."}}
  2. {{"agent": "analyst", "query": "Under ./agentx_dev, walk the tree with Path.rglob('*.py') (skip __pycache__/.venv/dist/build), then compute and print: (a) file count; (b) total LOC excluding blank+comment lines; (c) top-5 files by line count; (d) count of `class ` and `def ` occurrences per file; (e) TODO/FIXME/XXX matches with file:line. Include all numbers verbatim in your final answer."}}
  3. {{"agent": "reporter", "query": "Write ./workspace/codebase_analysis.md with the metrics above, if_exists='rename'."}}

WRONG plan (what NOT to do):
  1. {{"agent": "explorer", "query": "count Python files"}}
  2. {{"agent": "explorer", "query": "count total LOC"}}          ← explorer has no run_python → fails
  3. {{"agent": "explorer", "query": "top 5 by line count"}}      ← explorer has no run_python → fails
  ... (every computation step fails; the specialist spirals then aborts)

### Example B — one spawn, reused across many steps
The spawn step is CHEAP (just registers the specialist). One spawn + several dispatches to the spawned agent is normal. Do NOT spawn twice for the same capability.

## RULES
  - Only spawn when NO existing specialist can reasonably do the sub-task. Prefer reusing.
  - Grant only the capabilities the specialist genuinely needs.
  - A spawn step is its own step — it just registers the specialist; a follow-up step USES it.
  - Spawns may be REJECTED. If a spawn is rejected, later steps referring to it will fail — write the plan assuming approval, but know that rejection is possible.
────────────────────────────────────────────────────────────────────
"""


SUPERVISOR_PLAN_PROMPT = """You are a Supervisor. Your job is to write the SHORTEST plan that solves the user's task correctly. Fewer steps beat more steps.

Available specialist agents:
{agent_catalog}

User task: {user_task}

Rules for writing the plan (read these carefully — the shape of your plan matters more than any single word in it):

1. MINIMUM VIABLE PLAN. Every step is an independent LLM call — a full sub-agent invocation with its own reasoning loop, its own token cost, and its own chance of failure. Errors and cost compound with each step. Do NOT decompose a task into more steps just because you can. If the whole task fits one specialist's scope, use ONE step.

2. MERGE SEQUENTIAL WORK FOR THE SAME SPECIALIST. If two adjacent steps would both go to the same agent, they should almost always be ONE step. Bad: "write inspect.py" + "run inspect.py" + "report the output" (three python_agent calls). Good: "write inspect.py that scrapes X, run it, and report the title / link count / contacts it prints" (one python_agent call). The specialist's own reasoning loop handles the sequencing.

3. DECLARE DEPENDENCIES WITH depends_on. Give every step an "id" (a short snake_case name). When a step CONSUMES an earlier step's output, list that step's id in its "depends_on". The dispatched step then receives exactly those steps' findings as "PRIOR SUB-TASK FINDINGS" context (structured JSON when the earlier specialist emits typed output, otherwise its answer text). Dependency CHAINS ARE GOOD design: intent-analysis feeding retrieval feeding reranking is a strong plan.
   Rules:
   - depends_on lists DIRECT dependencies only, but ALL of them: if a step reads BOTH the intent analysis AND the retrieval output, it depends on both ids — transitive context is NOT forwarded automatically.
   - Do NOT chain independent steps. Steps with no dependency between them run IN PARALLEL — missing edges are what makes the plan fast. "Summarize topic A" and "summarize topic B" share no data: no depends_on between them.
   - Steps that would go to the SAME specialist back-to-back should still be merged into one (rule 2) — chains hand work BETWEEN different specialists, they don't split one specialist's work.
   - Write each dependent step's query as an instruction about what to DO with the prior findings ("using the intent analysis, retrieve the top 10 candidate passages"), never a request to re-state them.
   - A step may be skipped conditionally with "skip_when": {{"step": "<direct dep id>", "field": "<field on that step's typed output>", "is": <value>}}. Use it to short-circuit unnecessary work (e.g. skip retrieval when the intent step returns needs_rag=false). Only reference a DIRECT dependency and only fields its specialist actually returns (the catalog lists them).

4. SKIP SPECULATIVE HOUSEKEEPING. Don't add a "list files first to see what's there" step just because it feels safer. Specialists handle their own preconditions internally. Bad plan: (a) list ./workspace, (b) delete files in ./workspace, (c) write inspect.py. Good plan: (a) clear ./workspace and write inspect.py.

5. NO FINAL "REPORT" STEP. The framework already synthesizes all sub-task outputs into a final answer for the user after your plan runs. A sub-task whose query is "summarize what was found" or "report the results" is wasted — the synthesis step covers it. End your plan on the last step that does REAL WORK.

6. HIGHER STEP COUNTS ARE A RED FLAG. If your plan has 4+ steps, look again — you can probably merge two adjacent same-specialist steps. Only go higher when the sub-tasks genuinely need DIFFERENT specialists or CAN run in parallel.

Budget: at most {max_subtasks} sub-tasks. For most tasks 1–3 is right.

Respond ONLY with valid JSON in this exact format (no code fences, no extra text):

{{
  "plan": [
    {{"id": "<short_snake_case_id>", "agent": "<agent_name>", "query": "<specific sub-task>"}},
    {{"id": "<id2>", "agent": "<agent_name>", "query": "<sub-task consuming id1's output>", "depends_on": ["<short_snake_case_id>"]}},
    ...
  ]
}}

"id" is required on every step; "depends_on" and "skip_when" only where rule 3 calls for them. Steps without depends_on are independent roots and may run in parallel.
"""

SUPERVISOR_SYNTHESIZE_PROMPT = """You are answering the user's question directly, using ONLY the facts the specialists explicitly reported.

The user asked: {user_task}

The specialists reported back:
{results_block}

Write the user's final answer now. Guidelines:

- CRITICAL — NEVER FABRICATE. Every specific fact in your answer (titles, URLs, names, emails, phone numbers, tables, competitor lists, extracted values, code snippets) MUST come verbatim from the specialists' reports above. If a specialist only sent a status message ("wrote the file", "task complete", "saved to X") and did NOT include the actual data, treat that data as MISSING. Do NOT invent plausible-looking substitutes. Do NOT reconstruct what the file "probably" contains. Do NOT fill gaps from your own knowledge of the topic.

- WHEN DATA IS MISSING: say so plainly. "The report was saved to <path>; open that file to see the extracted details" is a fine answer if the specialist only reported the save path. A short honest answer beats a long fabricated one.

- Answer the user's question DIRECTLY. Lead with the answer, not with a recap of what steps ran.
- If the user asked for specific fields (title, count, emails, phone numbers, etc.), name each one explicitly — but only if a specialist actually reported that field.
- Filter out obvious garbage — the specialists' regexes / heuristics sometimes pull in false positives (e.g. CSS "font-weight" values matched as emails). Silently drop them if they're clearly not the real answer.
- Be concise. No preamble, no meta-commentary, no "based on the specialists' output". Just the answer.
- Do NOT mention the sub-agents, the plan, the orchestration process, or that multiple steps ran. To the user, this is one answer.
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Strip ```json / ``` code fences from an LLM response if present."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0]
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0]
    return cleaned.strip()


def _format_results_block(subtask_results: List[SubtaskResult]) -> str:
    """Format the sub-task results for inclusion in the synthesis prompt."""
    return "\n\n".join(
        f"[{r.agent}] Q: {r.query}\nA: {r.content}"
        + (f"\n  ERROR: {r.error}" if r.error else "")
        for r in subtask_results
    )


def _build_augmented_query(
    sub_query: str,
    prior_results: List[SubtaskResult],
    max_context_chars: int = 8000,
) -> str:
    """Prepend prior sub-task findings to a sub-query so the dispatched
    specialist has the context it needs.

    Without this, every specialist runs in isolation — step 3's
    'file_agent, write a report' has NO idea what step 2's researcher
    actually found, and the specialist either hallucinates content or
    (worse) refuses and asks for more info. Supervisor OWNS the results
    already; auto-injecting them is the natural fix.

    Rules:
      - Skip __spawn__ bookkeeping entries and errored steps (they add
        noise, not context).
      - Total context budget is bounded by ``max_context_chars`` —
        long HTML dumps get truncated per-entry with a marker, so the
        specialist can still act on the head of the data. Entries are
        kept in plan order.
      - If there are no useful prior results, return the query
        unchanged so the prompt shape matches the previous behavior.
    """
    useful = [
        r for r in prior_results
        if r.agent != "__spawn__" and not r.error and r.content
    ]
    if not useful:
        return sub_query

    # Distribute the context budget across entries so one giant scrape
    # can't starve later steps. Simple even split; small enough entries
    # that leave headroom get passed through in full.
    per_entry_cap = max(400, max_context_chars // max(1, len(useful)))
    blocks: List[str] = []
    for r in useful:
        # Structured output beats prose. When the prior specialist declared
        # an output_schema, hand the NEXT specialist the validated JSON
        # instead of (or in addition to) the display text — downstream
        # steps then parse fields, not sentences. The schema name is
        # included so the specialist knows what shape it is looking at.
        if r.output is not None:
            try:
                if hasattr(r.output, "model_dump_json"):
                    structured = r.output.model_dump_json(indent=2)
                    schema_name = type(r.output).__name__
                else:
                    structured = json.dumps(r.output, indent=2, default=repr)
                    schema_name = "data"
                content = (
                    f"STRUCTURED OUTPUT ({schema_name}):\n{structured}\n\n"
                    f"Summary text:\n{r.content}"
                )
            except Exception:
                content = r.content
        else:
            content = r.content
        if len(content) > per_entry_cap:
            content = content[:per_entry_cap] + f"\n... (truncated at {per_entry_cap} chars)"
        blocks.append(
            f"[{r.agent}] answered: {r.query}\n---\n{content}"
        )
    context = "\n\n".join(blocks)
    return (
        "PRIOR SUB-TASK FINDINGS (context from earlier steps in this "
        "plan — use them; do not ask the operator for data that's "
        f"already here):\n\n{context}\n\n"
        f"===\n\nYOUR SUB-TASK NOW:\n{sub_query}"
    )


def _augment_query_with_error(base_query: str, error: str, attempt: int) -> str:
    """Append a failed sub-task's error to its query so the specialist
    knows what to fix on the next attempt.

    The repair loop is INFORMED, not blind: a bare re-dispatch would very
    likely reproduce the same failure. Naming the error — and hinting at
    the usual culprits (malformed tool call, bad escaping) — gives the
    specialist's own reasoning loop something to correct against."""
    return (
        f"{base_query}\n\n===\n\n[supervisor] Your PREVIOUS attempt (#{attempt}) "
        f"FAILED with this error:\n{error}\n\nDiagnose the cause and try again. "
        f"If it was a malformed tool call or an unescaped backslash in a code "
        f"string / file path, fix the formatting and resend. Do not repeat the "
        f"same mistake."
    )


def _augment_query_with_unmet_criteria(
    base_query: str, reason: str, attempt: int,
) -> str:
    """Append a success-check rejection to a sub-task's query so the
    specialist knows its previous output was unacceptable and WHY.

    Distinct from the raised-exception path: here nothing crashed, the
    result just didn't meet the caller's bar. The emphasis is on
    *changing approach* — repeating the same method that produced an
    empty/insufficient result would produce it again."""
    return (
        f"{base_query}\n\n===\n\n[supervisor] Your PREVIOUS attempt (#{attempt}) "
        f"ran without error but did NOT satisfy the task's success criteria: "
        f"{reason}\n\nThe same approach will fail the same way — change your "
        f"METHOD (a different tool, endpoint, parse strategy, or fallback) and "
        f"produce output that meets the criteria."
    )


def _evaluate_success_verdict(verdict) -> tuple:
    """Normalize a ``subtask_success_check`` return value into
    ``(ok: bool, reason: str)``.

    Contract for the caller's predicate:
      - ``True``        → satisfactory.
      - ``False``       → unsatisfactory, generic reason.
      - non-empty str   → unsatisfactory, the string is the reason shown
                          to the specialist on retry.
      - any other value → truthiness decides ok; generic reason if not.
    """
    if verdict is True:
        return True, ""
    if isinstance(verdict, str):
        reason = verdict.strip()
        return (False, reason) if reason else (True, "")
    generic = "the sub-task output did not meet the required success criteria"
    return (True, "") if verdict else (False, generic)


# ----------------------------------------------------------------------------
# 3.3: plan-graph helpers (pure functions — unit-testable without a model)
# ----------------------------------------------------------------------------

def _render_agent_catalog(agents: Dict[str, Any]) -> str:
    """Render the planner-facing catalog. Specialist entries (3.3) get
    their extra metadata lines; classic tuples render as before."""
    lines: List[str] = []
    for name, entry in agents.items():
        if isinstance(entry, Specialist):
            lines.append(f"- {name}: {entry.description}")
            if entry.depends_on:
                lines.append(f"    typically after: {', '.join(entry.depends_on)}")
            if entry.output_schema is not None:
                try:
                    fields = ", ".join(entry.output_schema.model_fields.keys())
                    lines.append(f"    returns: {entry.output_schema.__name__}({fields})")
                except Exception:
                    lines.append(f"    returns: {getattr(entry.output_schema, '__name__', 'typed output')}")
            if entry.when_to_use:
                lines.append(f"    use when: {entry.when_to_use}")
        else:
            desc, _ = entry
            lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _plan_repair_note(repairs: List[str]) -> str:
    """Feedback block appended to the planning prompt on a replan after
    sanitization had to fix the previous attempt's graph."""
    bullet = "\n".join(f"- {r}" for r in repairs)
    return (
        "\n\nYOUR PREVIOUS PLAN HAD DEPENDENCY ERRORS that were auto-repaired:\n"
        f"{bullet}\n"
        "Write the plan again with a valid dependency graph: every "
        "depends_on entry must name an existing step id, no step may "
        "depend on itself or on a __spawn__ step, and the graph must "
        "contain no cycles."
    )


def _plan_uses_deps(plan: List[dict]) -> bool:
    """True when ANY step declares ``depends_on`` — the DAG-mode switch.
    A dep-free plan keeps byte-identical legacy semantics."""
    return any(
        isinstance(step, dict) and step.get("depends_on")
        for step in plan
    )


def _sanitize_plan(plan: List[dict], verbose: bool = False) -> Tuple[List[dict], List[str]]:
    """Normalize a planner-emitted plan into a valid DAG. Deterministic;
    never raises. Returns ``(plan, repairs)`` where ``repairs`` lists
    every fix made (empty = the plan was already clean). The list feeds
    the optional plan-repair replan loop.

    Rules, in order (see docs/design/3.3-depends-on-dag.md §3.1):
      1. auto-assign missing/duplicate ids as ``step_N`` (1-based position)
      2. drop depends_on entries naming unknown step ids
      3. drop self-dependencies
      4. break cycles by dropping the back-edge in plan order
      5. spawn steps cannot be depended on (such deps are dropped)
    """
    repairs: List[str] = []
    plan = [dict(step) for step in plan if isinstance(step, dict)]

    # -- 1. ids ------------------------------------------------------------
    seen: set = set()
    for i, step in enumerate(plan, 1):
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip() or sid in seen:
            new_id = f"step_{i}"
            # Extremely defensive: if the auto-name itself collides with a
            # planner-chosen id, suffix until unique.
            while new_id in seen:
                new_id += "_"
            if sid in seen:
                repairs.append(f"duplicate id {sid!r} at position {i} renamed to {new_id!r}")
            step["id"] = new_id
        seen.add(step["id"])

    ids = [s["id"] for s in plan]
    id_pos = {sid: i for i, sid in enumerate(ids)}
    spawn_ids = {s["id"] for s in plan if s.get("agent") == "__spawn__"}

    # -- 2/3/5. dep validation ----------------------------------------------
    for step in plan:
        deps = step.get("depends_on") or []
        if not isinstance(deps, list):
            repairs.append(f"step {step['id']!r}: depends_on was not a list -- dropped")
            step["depends_on"] = []
            continue
        clean: List[str] = []
        for d in deps:
            if d == step["id"]:
                repairs.append(f"step {step['id']!r}: self-dependency dropped")
            elif d not in id_pos:
                repairs.append(f"step {step['id']!r}: unknown dependency {d!r} dropped")
            elif d in spawn_ids:
                repairs.append(
                    f"step {step['id']!r}: dependency on spawn step {d!r} dropped "
                    f"(spawn steps are bookkeeping, not data producers)"
                )
            elif d not in clean:
                clean.append(d)
        step["depends_on"] = clean

    # -- 4. cycle breaking (Kahn's; drop the back-edge in plan order) -------
    while True:
        indeg = {sid: 0 for sid in ids}
        dependents: Dict[str, List[str]] = {sid: [] for sid in ids}
        for step in plan:
            for d in step["depends_on"]:
                indeg[step["id"]] += 1
                dependents[d].append(step["id"])
        queue = [sid for sid in ids if indeg[sid] == 0]
        visited = 0
        qi = 0
        while qi < len(queue):
            sid = queue[qi]; qi += 1
            visited += 1
            for dep_id in dependents[sid]:
                indeg[dep_id] -= 1
                if indeg[dep_id] == 0:
                    queue.append(dep_id)
        if visited == len(ids):
            break
        # Cycle exists. Among cyclic nodes, find the edge whose SOURCE is
        # latest in plan order and TARGET earliest — the back-edge — and
        # drop it. Plan order is the planner's own statement of intended
        # sequence, so the forward reading survives.
        cyclic = {sid for sid in ids if indeg[sid] > 0}
        back_edge = None   # (source_dep, step_id) — step depends_on source
        for step in plan:
            if step["id"] not in cyclic:
                continue
            for d in step["depends_on"]:
                if d in cyclic and id_pos[d] > id_pos[step["id"]]:
                    cand = (d, step["id"])
                    if back_edge is None or id_pos[d] > id_pos[back_edge[0]]:
                        back_edge = cand
        if back_edge is None:
            # Pure forward-edge cycle can't exist; belt-and-braces: drop
            # the first cyclic step's first dep so the loop terminates.
            for step in plan:
                if step["id"] in cyclic and step["depends_on"]:
                    back_edge = (step["depends_on"][0], step["id"])
                    break
        src, tgt = back_edge
        plan[[s["id"] for s in plan].index(tgt)]["depends_on"].remove(src)
        repairs.append(f"cycle broken: dropped dependency {src!r} from step {tgt!r}")

    if repairs and verbose:
        for r in repairs:
            print(f"{_C_ERROR}[supervisor.plan] repaired: {r}{_C_RESET}")
    return plan, repairs


def _topo_order(plan: List[dict]) -> List[int]:
    """Stable topological order over plan indices: a step never precedes
    its dependencies, and ties break by plan position. Assumes the plan
    has been through ``_sanitize_plan`` (acyclic, valid ids)."""
    ids = [s["id"] for s in plan]
    id_idx = {sid: i for i, sid in enumerate(ids)}
    indeg = [len(s.get("depends_on") or []) for s in plan]
    dependents: List[List[int]] = [[] for _ in plan]
    for i, step in enumerate(plan):
        for d in step.get("depends_on") or []:
            dependents[id_idx[d]].append(i)

    import heapq
    ready = [i for i, deg in enumerate(indeg) if deg == 0]
    heapq.heapify(ready)
    order: List[int] = []
    while ready:
        i = heapq.heappop(ready)   # smallest plan index first → stable
        order.append(i)
        for j in dependents[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(ready, j)
    return order


def _evaluate_skip_when(
    cond: Any,
    results_by_id: Dict[str, SubtaskResult],
    step_deps: List[str],
    verbose: bool = False,
) -> Tuple[bool, str]:
    """Evaluate a step's ``skip_when`` condition against a DIRECT
    dependency's structured output. Returns ``(skip, reason)``.

    FAIL-OPEN by design: malformed condition, non-dep step reference,
    missing output, missing field, comparison error — every failure
    path returns ``(False, ...)`` and the step RUNS. A skip must be
    provably justified. Single operator: ``is`` (equality). Dotted
    field paths supported (``"meta.confidence"``).
    """
    if not isinstance(cond, dict):
        return False, ""
    ref = cond.get("step")
    fld = cond.get("field")
    if "is" not in cond or not isinstance(ref, str) or not isinstance(fld, str):
        return False, ""
    if ref not in step_deps:
        if verbose:
            print(f"{_C_ERROR}[supervisor.skip_when] step {ref!r} is not a "
                  f"direct dependency -- condition ignored (fail-open){_C_RESET}")
        return False, ""
    dep = results_by_id.get(ref)
    if dep is None or dep.output is None:
        return False, ""
    try:
        value: Any = dep.output
        for part in fld.split("."):
            if isinstance(value, dict):
                value = value[part]
            else:
                value = getattr(value, part)
        if value == cond["is"]:
            return True, f"{ref}.{fld} == {cond['is']!r}"
    except Exception:
        return False, ""
    return False, ""


# ANSI colors match the AgentRunner's verbose output so a mixed
# supervisor + inner-runner trace reads consistently.
_C_PLAN     = "\x1B[1;34m"   # blue bold  — plan header + steps
_C_DISPATCH = "\x1B[3;33m"   # yellow italic — 'dispatching to X'
_C_RESULT   = "\x1B[32m"     # green — sub-task result summary
_C_FINAL    = "\x1B[1;32m"   # green bold — synthesized final answer
_C_ERROR    = "\x1B[1;31m"   # red bold — sub-task error
_C_RESET    = "\x1B[0m"


def _log_plan(plan: List[dict]) -> None:
    print(f"{_C_PLAN}[supervisor.plan] {len(plan)} step(s):{_C_RESET}")
    for i, step in enumerate(plan, 1):
        agent = step.get("agent", "?")
        query = step.get("query", "")
        print(f"{_C_PLAN}  {i}. [{agent}]{_C_RESET} {query}")


def _log_dispatch(agent_name: str, sub_query: str) -> None:
    preview = sub_query if len(sub_query) < 120 else sub_query[:117] + "..."
    print(f"{_C_DISPATCH}[supervisor.dispatch -> {agent_name}]{_C_RESET} {preview}")


def _log_result(result: SubtaskResult) -> None:
    if result.error:
        print(f"{_C_ERROR}[supervisor.result <- {result.agent}] ERROR:{_C_RESET} {result.error}")
        return
    preview = result.content
    if len(preview) > 300:
        preview = preview[:300] + f"... ({len(result.content)} chars total)"
    print(f"{_C_RESULT}[supervisor.result <- {result.agent}]{_C_RESET} {preview}")


def _log_final(final: str) -> None:
    print(f"{_C_FINAL}[supervisor.final]{_C_RESET} {final}")


def _log_no_plan() -> None:
    print(f"{_C_ERROR}[supervisor.plan] failed to produce a valid plan{_C_RESET}")


# ----------------------------------------------------------------------------
# Sync Supervisor
# ----------------------------------------------------------------------------

class Supervisor:
    """
    Synchronous multi-agent supervisor.

    Decomposes a high-level task into sub-tasks, dispatches them to the
    appropriate specialist :class:`AgentRunner`, and synthesizes the
    results into a single final answer.
    """

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, AgentRunner]],
        max_subtasks: int = 5,
        verbose: bool = True,
        spawn_config: Optional[SpawnConfig] = None,
        max_subtask_retries: int = 1,
        subtask_success_check: Optional[Callable[[SubtaskResult], Any]] = None,
        max_plan_retries: int = 1,
    ):
        """
        Args:
            model: LLM used for planning and synthesis.
            agents: Mapping of ``name -> (description, AgentRunner)`` or
                ``name -> Specialist`` (3.3). Tuples are wrapped as
                Specialist internally.
            max_subtasks: Hard upper bound on the number of planned sub-tasks.
            verbose: When True (default), print colored progress markers for
                each stage — the plan, each sub-task dispatch and result,
                and the synthesized final answer. Set to False for silent
                operation (only the SupervisorResult is returned).
            spawn_config: Optional SpawnConfig enabling the planner to
                request NEW specialists mid-plan. When None (default),
                spawning is disabled and the planner can only use the
                specialists passed in ``agents``. See SpawnConfig for the
                auto_spawn / approver knobs.
            max_subtask_retries: How many times a sub-task that RAISES is
                re-dispatched before the Supervisor gives up on it.
                Default 1 (so a specialist that hits a transient or
                self-correctable failure — a malformed tool call, a bad
                escape — gets a second chance instead of the whole
                sub-task being abandoned on the first error). Each retry
                appends the prior error to the query so the specialist
                knows what to fix. Set to 0 to restore the old
                quit-on-first-failure behavior. Only errors trigger a
                retry; a sub-task that returns content (even thin content)
                is accepted as-is — unless you also pass
                ``subtask_success_check`` (below).
            subtask_success_check: Optional predicate
                ``(SubtaskResult) -> bool | str`` deciding whether a
                *returned* (non-raised) result is acceptable. Catches the
                "ran fine but produced nothing useful" case — a scraper
                that saved 0 links, an extractor that found no data.
                Return ``True`` to accept; ``False`` or a ``str`` reason
                to reject. A rejected result is retried like a raised
                error (the reason is fed back into the query), bounded by
                ``max_subtask_retries``; once exhausted the last result is
                returned with its ``error`` set to the reason (content
                preserved). A predicate that itself raises is treated as
                "accept" so a buggy check can't wedge the run. Default
                ``None`` keeps the exceptions-only behavior.
            max_plan_retries: (3.3) When plan sanitization has to repair
                the planner's graph (unknown dependency dropped, cycle
                broken), the plan is re-requested up to this many times
                with the repair warnings appended to the prompt. The
                sanitized plan is kept as fallback if the retry is no
                better. ``0`` disables replanning.
        """
        self.model = model
        # Normalize (and copy) so run-time spawns don't mutate the
        # caller's dict. Accepts (description, runner) tuples or
        # Specialist entries (3.3); everything is stored as Specialist,
        # which still tuple-unpacks for older code.
        self.agents = _normalize_agents(agents)
        self.max_subtasks = max_subtasks
        self.verbose = verbose
        self.spawn_config = spawn_config or SpawnConfig(enabled=False)
        self._spawns_this_run = 0
        self.max_subtask_retries = max(0, int(max_subtask_retries))
        self.subtask_success_check = subtask_success_check
        self.max_plan_retries = max(0, int(max_plan_retries))

    # -- internal helpers ----------------------------------------------------

    def _evaluate_success(self, result: SubtaskResult) -> tuple:
        """Run ``subtask_success_check`` against a returned result.
        Returns ``(ok, reason)``. No check configured → always ``(True, "")``.
        A check that raises is swallowed (treated as pass) so a broken
        predicate can't wedge the run."""
        if self.subtask_success_check is None:
            return True, ""
        try:
            verdict = self.subtask_success_check(result)
        except Exception as e:
            logger.warning(f"subtask_success_check raised; treating as pass: {e}")
            return True, ""
        return _evaluate_success_verdict(verdict)

    def _build_agent_catalog(self) -> str:
        return _render_agent_catalog(self.agents)

    def _plan_once(self, user_task: str, repair_note: str = "") -> List[dict]:
        base_prompt = SUPERVISOR_PLAN_PROMPT.format(
            agent_catalog=self._build_agent_catalog(),
            user_task=user_task,
            max_subtasks=self.max_subtasks,
        )
        prompt = base_prompt
        if self.spawn_config.enabled:
            prompt = prompt + SUPERVISOR_SPAWN_INSTRUCTION
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = self.model.Initialize(messages=messages)

        cleaned = _strip_code_fences(response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        plan = parsed.get("plan", []) or []
        # Keep spawn steps AND known-agent steps. Unknown agents at this
        # stage might refer to a specialist a later spawn step will
        # create; validate order in run() rather than dropping them here.
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and (item.get("agent") == "__spawn__" or item.get("agent") or item.get("query"))
        ]
        return filtered[: self.max_subtasks]

    def _plan(self, user_task: str) -> List[dict]:
        """Plan, sanitize, and (3.3) replan once per repair budget when
        sanitization had to fix the graph. Returns a sanitized plan whose
        every step carries a valid ``id`` and acyclic ``depends_on``."""
        plan = self._plan_once(user_task)
        if not plan:
            return []
        sane, repairs = _sanitize_plan(plan, verbose=self.verbose)
        retries = self.max_plan_retries
        while repairs and retries > 0:
            retries -= 1
            note = _plan_repair_note(repairs)
            retry_plan = self._plan_once(user_task, repair_note=note)
            if not retry_plan:
                break
            retry_sane, retry_repairs = _sanitize_plan(retry_plan, verbose=self.verbose)
            if len(retry_repairs) < len(repairs):
                sane, repairs = retry_sane, retry_repairs
            else:
                break   # retry was no better; keep the sanitized original
        return sane

    # Map from SpawnRequest capability keyword -> the concrete tool names
    # a spawn would install. Kept in sync with _build_spawned_agent's
    # capability dispatch. Used by _find_existing_for_capabilities to
    # detect duplicate-capability spawns before they happen.
    _CAP_TO_TOOLS: Dict[str, set] = {
        "web": {"web_search", "web_fetch"},
        "files": {"read_path", "write_file", "edit_file", "list_directory"},
        "code": {"run_python"},
        "delete": {"delete_path"},
    }

    def _find_existing_for_capabilities(
        self, capabilities: List[str],
    ) -> Optional[str]:
        """If any registered specialist ALREADY has every tool the
        requested capabilities would install, return that specialist's
        name. Used to refuse redundant spawns.

        Uses concrete tool-name overlap, not description matching — a
        specialist that WAS spawned with 'web' has web_search + web_fetch
        registered on its runner, so the check works whether the
        specialist was pre-built or previously spawned.

        Returns None if no existing specialist covers the caps, or if
        capabilities is empty (nothing to check)."""
        required: set = set()
        for c in capabilities:
            required |= self._CAP_TO_TOOLS.get(c.lower().strip(), set())
        if not required:
            return None
        for name, (_desc, runner) in self.agents.items():
            agent_tool_names = {getattr(t, "name", None) for t in getattr(runner, "tools", [])}
            if required.issubset(agent_tool_names):
                return name
        return None

    def _handle_spawn(self, spawn_step: dict) -> Tuple[Optional[str], Optional[str]]:
        """Process a __spawn__ step. Returns a tuple:
            (name_to_use, rewrite_from)

        - New spawn approved      -> (new_name, None)
        - Refused due to overlap  -> (existing_name, requested_name)
              The Supervisor should REWRITE any subsequent plan steps
              that reference `requested_name` to use `existing_name`
              instead — this prevents the "UNKNOWN AGENT — skipping"
              downstream failure that leaves the reporter with no data
              and forces it to fabricate content.
        - Any other refusal       -> (None, None)

        Emits verbose logs on every branch so the trace shows exactly
        which decision was made."""
        cfg = self.spawn_config
        if not cfg.enabled:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] request ignored — "
                      f"spawn_config.enabled=False{_C_RESET}")
            return None, None
        if self._spawns_this_run >= cfg.max_spawns:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] refused — max_spawns "
                      f"({cfg.max_spawns}) already reached{_C_RESET}")
            return None, None

        req = SpawnRequest(
            name=str(spawn_step.get("name", "")).strip(),
            description=str(spawn_step.get("description", "")).strip(),
            capabilities=[str(c).strip() for c in
                          (spawn_step.get("capabilities") or []) if c],
            rationale=str(spawn_step.get("rationale", "")).strip(),
        )
        if not req.name or not req.description:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] malformed request — "
                      f"missing name or description{_C_RESET}")
            return None, None
        if req.name in self.agents:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] name '{req.name}' already "
                      f"registered — refusing to overwrite{_C_RESET}")
            return None, None

        # Duplicate-capability guard: check the existing catalog. If an
        # existing specialist already covers the requested caps, refuse
        # the spawn AND return the existing specialist's name so run()
        # can rewrite subsequent plan steps that reference req.name to
        # use the existing specialist. Without the rewrite, follow-up
        # dispatches to req.name would hit "UNKNOWN AGENT — skipping"
        # and downstream steps (like a reporter that needed the
        # research data) would fabricate content.
        existing = self._find_existing_for_capabilities(req.capabilities)
        if existing is not None:
            if self.verbose:
                print(f"{_C_PLAN}[supervisor.spawn] REFUSED spawn '{req.name}' — "
                      f"existing specialist '{existing}' already covers "
                      f"caps {req.capabilities}. AUTO-REROUTING subsequent "
                      f"dispatches from '{req.name}' → '{existing}'.{_C_RESET}")
            return existing, req.name

        if cfg.auto_spawn:
            # Security gate: if auto_spawn_allowed_caps is configured,
            # every requested cap MUST be in it. Anything else drops to
            # the human approver (or refusal if none). This blocks a
            # prompt-injected task from silently getting `code`/`delete`
            # under auto_spawn — see SpawnConfig docstring for rationale.
            asked = {c.lower() for c in req.capabilities}
            allow = cfg.auto_spawn_allowed_caps
            if allow is not None and not asked.issubset(allow):
                over_reach = sorted(asked - allow)
                if self.verbose:
                    print(f"{_C_ERROR}[supervisor.spawn] '{req.name}' — "
                          f"auto-spawn REFUSED; caps {over_reach} not in "
                          f"auto_spawn_allowed_caps={sorted(allow)}. "
                          f"Falling through to approver.{_C_RESET}")
                if cfg.approver is None:
                    # No human approver configured and auto-spawn refused
                    # this request. Fail closed rather than silently
                    # granting.
                    if self.verbose:
                        print(f"{_C_ERROR}[supervisor.spawn] REJECTED — "
                              f"no approver set and auto-spawn refused "
                              f"cap set {over_reach}.{_C_RESET}")
                    return None, None
                approved = bool(cfg.approver(req))
                if self.verbose:
                    verdict = f"{_C_RESULT}approved" if approved else f"{_C_ERROR}rejected"
                    print(f"[supervisor.spawn] {verdict}{_C_RESET}")
            else:
                approved = True
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.spawn] auto-approving '{req.name}' "
                          f"(caps: {', '.join(req.capabilities) or 'none'}){_C_RESET}")
        else:
            approver = cfg.approver or _default_interactive_approver
            if self.verbose and cfg.approver is None:
                # Only announce this when using the default interactive
                # approver; a custom approver may handle its own UI.
                print(f"{_C_DISPATCH}[supervisor.spawn] '{req.name}' — requesting "
                      f"human approval{_C_RESET}")
            approved = bool(approver(req))
            if self.verbose:
                verdict = f"{_C_RESULT}approved" if approved else f"{_C_ERROR}rejected"
                print(f"[supervisor.spawn] {verdict}{_C_RESET}")

        if not approved:
            return None, None

        description, runner = _build_spawned_agent(
            req, model=self.model, allowed_paths=cfg.allowed_paths,
        )
        self.agents[req.name] = Specialist(description=description, runner=runner)
        self._spawns_this_run += 1
        return req.name, None

    def _dispatch_with_retry(
        self,
        agent_runner: AgentRunner,
        agent_name: str,
        sub_query: str,
        dispatched_query: str,
    ) -> SubtaskResult:
        """Run one sub-task, retrying up to ``max_subtask_retries`` times.

        Two kinds of failure are retried, each feeding context back into
        the query:
          - a RAISED exception (crash, unrecoverable tool error), and
          - a returned result that ``subtask_success_check`` rejects
            (ran fine but produced nothing useful).

        Without a success check, only raised exceptions retry — a
        sub-task that returns is accepted even if thin, since the
        Supervisor can't tell "terse but correct" from "wrong" on its own.

        On exhaustion: returns the last result with ``error`` set (content
        preserved) if we got one, else an empty errored SubtaskResult."""
        attempts = self.max_subtask_retries + 1
        query = dispatched_query
        last_error = ""
        last_result: Optional[SubtaskResult] = None
        for attempt in range(1, attempts + 1):
            try:
                completion = agent_runner.Initialize(query)
                result = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    # Preserve the runner's validated Pydantic instance so
                    # downstream steps get typed data, not just prose.
                    output=getattr(completion, "output", None),
                )
            except Exception as e:
                last_error = str(e)
                last_result = None
                if self.verbose:
                    print(
                        f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                        f"attempt {attempt}/{attempts} failed: {last_error}"
                        f"{_C_RESET}"
                    )
                if attempt < attempts:
                    query = _augment_query_with_error(
                        dispatched_query, last_error, attempt,
                    )
                continue

            # No exception — now apply the caller's success criteria.
            ok, reason = self._evaluate_success(result)
            if ok:
                return result
            last_result = result
            last_error = f"did not meet success criteria: {reason}"
            if self.verbose:
                print(
                    f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                    f"attempt {attempt}/{attempts} rejected: {reason}"
                    f"{_C_RESET}"
                )
            if attempt < attempts:
                query = _augment_query_with_unmet_criteria(
                    dispatched_query, reason, attempt,
                )

        if last_result is not None:
            # Keep the content the specialist produced, but flag it failed.
            last_result.error = last_error
            return last_result
        return SubtaskResult(
            agent=agent_name, query=sub_query, content="", error=last_error,
        )

    def _synthesize(self, user_task: str, subtask_results: List[SubtaskResult]) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        messages = [{"role": "user", "content": prompt}]
        return self.model.Initialize(messages=messages)

    # -- public API ----------------------------------------------------------

    def stream(self, user_task: str):
        """Yield structured events as the supervisor plans, dispatches,
        and synthesizes.

        Event shapes:
          - {"type": "plan_start"}
          - {"type": "plan",           "plan": <list of steps>}
          - {"type": "spawn",          "name": str, "capabilities": list}
          - {"type": "dispatch",       "agent": str, "query": str, "step": int}
          - {"type": "subtask_result", "result": SubtaskResult, "step": int}
          - {"type": "synthesize_start"}
          - {"type": "final",          "content": str}
          - {"type": "completion",     "result": SupervisorResult}   (last)

        The last event is always ``completion`` so ``run()`` can build
        on top of this and callers who want live UI updates can consume
        the intermediate events.
        """
        self._spawns_this_run = 0

        yield {"type": "plan_start"}
        plan = self._plan(user_task)

        if not plan:
            if self.verbose:
                _log_no_plan()
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
            )
            yield {"type": "final", "content": result.content}
            yield {"type": "completion", "result": result}
            return

        yield {"type": "plan", "plan": plan}
        if self.verbose:
            _log_plan(plan)

        # 3.3: DAG mode fires when ANY step declares depends_on. Dep-free
        # plans keep byte-identical legacy semantics (plan order, every
        # step sees ALL prior results).
        dag_mode = _plan_uses_deps(plan)
        order = _topo_order(plan) if dag_mode else list(range(len(plan)))

        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
        for step_idx in order:
            item = plan[step_idx]
            agent_name = item.get("agent")
            step_id = item.get("id") or f"step_{step_idx + 1}"
            step_deps = list(item.get("depends_on") or [])

            if agent_name == "__spawn__":
                spawned_name, rewrite_from = self._handle_spawn(item)
                yield {"type": "spawn", "name": spawned_name or item.get("name", "?"),
                       "capabilities": item.get("capabilities", []),
                       "rerouted_from": rewrite_from}
                if spawned_name and rewrite_from:
                    spawn_rewrites[rewrite_from] = spawned_name
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {rewrite_from}",
                        content=f"REROUTED: caps already covered by existing "
                                f"specialist '{spawned_name}'. Subsequent "
                                f"dispatches to '{rewrite_from}' will run "
                                f"on '{spawned_name}'.",
                        step_id=step_id,
                    )
                elif spawned_name:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {item.get('name', '?')}",
                        content=f"registered new specialist '{spawned_name}' with "
                                f"capabilities: {', '.join(item.get('capabilities', []))}",
                        step_id=step_id,
                    )
                else:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {item.get('name', '?')}",
                        content="",
                        error="spawn refused (see log for reason)",
                        step_id=step_id,
                    )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                continue

            # 3.3 failure cascade: a FAILED direct dependency (error set,
            # and not a mere condition-skip) skips this step. Condition-
            # skipped deps count as empty successes -- the step still runs.
            failed_dep = next(
                (d for d in step_deps
                 if d in results_by_id
                 and results_by_id[d].error
                 ), None,
            )
            if failed_dep is not None:
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content="",
                    error=(f"skipped: dependency '{failed_dep}' failed "
                           f"({results_by_id[failed_dep].error})"),
                    skipped=True,
                    step_id=step_id,
                    depends_on=step_deps,
                )
                if self.verbose:
                    print(f"{_C_ERROR}[supervisor.skip -> {agent_name}] "
                          f"dependency '{failed_dep}' failed{_C_RESET}")
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            # 3.3 conditional skip: evaluated against a direct dep's typed
            # output; fail-open. Does NOT cascade -- dependents treat this
            # step as an empty success.
            skip, reason = _evaluate_skip_when(
                item.get("skip_when"), results_by_id, step_deps, self.verbose,
            )
            if skip:
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content=f"skipped: {reason}",
                    skipped=True,
                    step_id=step_id,
                    depends_on=step_deps,
                )
                if self.verbose:
                    print(f"{_C_DISPATCH}[supervisor.skip -> {agent_name}] "
                          f"{reason}{_C_RESET}")
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            if agent_name in spawn_rewrites:
                original = agent_name
                agent_name = spawn_rewrites[agent_name]
                if self.verbose:
                    print(f"{_C_DISPATCH}[supervisor.dispatch] rewriting "
                          f"'{original}' -> '{agent_name}' (spawn was rerouted)"
                          f"{_C_RESET}")

            if agent_name not in self.agents:
                if self.verbose:
                    print(f"{_C_ERROR}[supervisor.dispatch -> {agent_name}] "
                          f"UNKNOWN AGENT -- skipping{_C_RESET}")
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content="",
                    error=f"specialist '{agent_name}' not in registry (spawn may have been refused)",
                    step_id=step_id,
                    depends_on=step_deps,
                )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            sub_query = item["query"]
            _, agent_runner = self.agents[agent_name]
            # Context routing: DAG mode threads DIRECT dependencies only
            # (explicit routing, focused context budget); legacy mode
            # threads everything prior, exactly as pre-3.3.
            if dag_mode:
                dep_results = [
                    results_by_id[d] for d in step_deps if d in results_by_id
                ]
                dispatched_query = _build_augmented_query(sub_query, dep_results)
            else:
                dispatched_query = _build_augmented_query(sub_query, subtask_results)
            yield {"type": "dispatch", "agent": agent_name, "query": sub_query,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_dispatch(agent_name, sub_query)
            sub_result = self._dispatch_with_retry(
                agent_runner, agent_name, sub_query, dispatched_query,
            )
            sub_result.step_id = step_id
            sub_result.depends_on = step_deps
            subtask_results.append(sub_result)
            results_by_id[step_id] = sub_result
            yield {"type": "subtask_result", "result": sub_result,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_result(sub_result)

        yield {"type": "synthesize_start"}
        final = self._synthesize(user_task, subtask_results)
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
        )
        yield {"type": "final", "content": final}
        yield {"type": "completion", "result": result}

    def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks sequentially, and synthesize.

        Thin wrapper over ``stream()``. Consumes every event and returns
        the final ``SupervisorResult``. For live UI progress, use
        ``stream()`` directly.
        """
        result: Optional[SupervisorResult] = None
        for event in self.stream(user_task):
            if event["type"] == "completion":
                result = event["result"]
        assert result is not None, "supervisor.stream() must yield a completion event"
        return result


    def __repr__(self) -> str:
        return (
            f"<Supervisor(agents={list(self.agents.keys())}, "
            f"max_subtasks={self.max_subtasks})>"
        )


# ----------------------------------------------------------------------------
# Async Supervisor
# ----------------------------------------------------------------------------

class AsyncSupervisor:
    """
    Asynchronous multi-agent supervisor.

    Like :class:`Supervisor` but runs sub-tasks concurrently via
    ``asyncio.gather``. Accepts both :class:`AsyncAgentRunner` and
    :class:`AgentRunner` instances (sync runners are wrapped in
    ``asyncio.to_thread``).
    """

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, Union[AgentRunner, AsyncAgentRunner]]],
        max_subtasks: int = 5,
        verbose: bool = True,
        sequential: bool = False,
        max_subtask_retries: int = 1,
        subtask_success_check: Optional[Callable[[SubtaskResult], Any]] = None,
        max_parallel: Optional[int] = None,
        max_plan_retries: int = 1,
    ):
        """
        Args:
            model: LLM used for planning and synthesis.
            agents: Mapping of ``name -> (description, AgentRunner)`` or
                ``name -> Specialist`` (3.3).
            max_subtasks: Hard upper bound on the number of planned sub-tasks.
            verbose: Print colored progress markers for each stage.
            sequential: When True, sub-tasks run one at a time in plan
                order and each specialist receives the prior sub-tasks'
                findings as context (sugar for ``max_parallel=1`` with
                all-prior threading). Default False: dep-free plans run
                fully concurrent as before; plans WITH ``depends_on``
                (3.3) run as a DAG — independent steps concurrent, each
                step threaded ONLY its direct dependencies' results, and
                a step starts the moment its dependencies complete.
            max_subtask_retries: How many times a sub-task that RAISES is
                re-dispatched (with the prior error appended) before the
                Supervisor gives up on it. Default 1. See
                :class:`Supervisor` for the full rationale. Applies in
                both sequential and concurrent modes — each sub-task
                retries independently.
            subtask_success_check: Optional ``(SubtaskResult) -> bool | str``
                predicate that also retries a *returned* result the check
                rejects (ran fine but produced nothing useful). See
                :class:`Supervisor` for the contract. Applies per sub-task
                in both sequential and concurrent modes.
            max_parallel: (3.3) Cap on concurrently running sub-tasks.
                ``None`` (default) = unbounded. Useful under provider
                rate limits. ``sequential=True`` forces this to 1.
            max_plan_retries: (3.3) Replans after sanitization repairs;
                see :class:`Supervisor`.
        """
        self.model = model
        self.agents = _normalize_agents(agents)
        self.max_subtasks = max_subtasks
        self.verbose = verbose
        self.sequential = sequential
        self.max_subtask_retries = max(0, int(max_subtask_retries))
        self.subtask_success_check = subtask_success_check
        self.max_parallel = 1 if sequential else (
            max(1, int(max_parallel)) if max_parallel is not None else None
        )
        self.max_plan_retries = max(0, int(max_plan_retries))

    def _evaluate_success(self, result: SubtaskResult) -> tuple:
        """See ``Supervisor._evaluate_success``."""
        if self.subtask_success_check is None:
            return True, ""
        try:
            verdict = self.subtask_success_check(result)
        except Exception as e:
            logger.warning(f"subtask_success_check raised; treating as pass: {e}")
            return True, ""
        return _evaluate_success_verdict(verdict)

    # -- internal helpers ----------------------------------------------------

    def _build_agent_catalog(self) -> str:
        return _render_agent_catalog(self.agents)

    async def _call_model(self, messages: List[Dict[str, str]]) -> str:
        """Invoke the planning/synthesis model, preferring an async method."""
        if hasattr(self.model, "async_initialize") and asyncio.iscoroutinefunction(
            self.model.async_initialize
        ):
            return await self.model.async_initialize(messages=messages)
        # Fall back to running the sync method in a thread.
        return await asyncio.to_thread(self.model.Initialize, messages)

    async def _plan_once(self, user_task: str, repair_note: str = "") -> List[dict]:
        prompt = SUPERVISOR_PLAN_PROMPT.format(
            agent_catalog=self._build_agent_catalog(),
            user_task=user_task,
            max_subtasks=self.max_subtasks,
        )
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = await self._call_model(messages)

        cleaned = _strip_code_fences(response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        plan = parsed.get("plan", []) or []
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and item.get("agent") in self.agents
            and item.get("query")
        ]
        return filtered[: self.max_subtasks]

    async def _plan(self, user_task: str) -> List[dict]:
        """Plan + sanitize + (3.3) replan-on-repair. See Supervisor._plan."""
        plan = await self._plan_once(user_task)
        if not plan:
            return []
        sane, repairs = _sanitize_plan(plan, verbose=self.verbose)
        retries = self.max_plan_retries
        while repairs and retries > 0:
            retries -= 1
            retry_plan = await self._plan_once(
                user_task, repair_note=_plan_repair_note(repairs),
            )
            if not retry_plan:
                break
            retry_sane, retry_repairs = _sanitize_plan(retry_plan, verbose=self.verbose)
            if len(retry_repairs) < len(repairs):
                sane, repairs = retry_sane, retry_repairs
            else:
                break
        return sane

    async def _synthesize(
        self, user_task: str, subtask_results: List[SubtaskResult]
    ) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        messages = [{"role": "user", "content": prompt}]
        return await self._call_model(messages)

    async def _run_subtask(
        self,
        agent_name: str,
        sub_query: str,
        prior_results: Optional[List[SubtaskResult]] = None,
    ) -> SubtaskResult:
        _, runner = self.agents[agent_name]
        # In sequential mode the caller passes findings-so-far; in
        # concurrent mode there's nothing to thread and the specialist
        # runs on the plain query. Stored result uses the plain query
        # so the audit trail isn't polluted with the injected context.
        dispatched_query = (
            _build_augmented_query(sub_query, prior_results)
            if prior_results else sub_query
        )
        if self.verbose:
            _log_dispatch(agent_name, sub_query)

        initialize = getattr(runner, "Initialize", None)
        if initialize is None:
            # Config error, not a transient failure — don't burn retries.
            result = SubtaskResult(
                agent=agent_name, query=sub_query, content="",
                error=f"Registered agent '{agent_name}' has no 'Initialize' method.",
            )
            if self.verbose:
                _log_result(result)
            return result

        attempts = self.max_subtask_retries + 1
        query = dispatched_query
        last_error = ""
        result: Optional[SubtaskResult] = None
        last_result: Optional[SubtaskResult] = None
        for attempt in range(1, attempts + 1):
            try:
                if asyncio.iscoroutinefunction(initialize):
                    completion = await initialize(query)
                else:
                    completion = await asyncio.to_thread(initialize, query)
                candidate = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    output=getattr(completion, "output", None),
                )
            except Exception as e:
                last_error = str(e)
                last_result = None
                if self.verbose:
                    print(
                        f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                        f"attempt {attempt}/{attempts} failed: {last_error}"
                        f"{_C_RESET}"
                    )
                if attempt < attempts:
                    query = _augment_query_with_error(
                        dispatched_query, last_error, attempt,
                    )
                continue

            ok, reason = self._evaluate_success(candidate)
            if ok:
                result = candidate
                break
            last_result = candidate
            last_error = f"did not meet success criteria: {reason}"
            if self.verbose:
                print(
                    f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                    f"attempt {attempt}/{attempts} rejected: {reason}"
                    f"{_C_RESET}"
                )
            if attempt < attempts:
                query = _augment_query_with_unmet_criteria(
                    dispatched_query, reason, attempt,
                )
        if result is None:
            if last_result is not None:
                last_result.error = last_error
                result = last_result
            else:
                result = SubtaskResult(
                    agent=agent_name, query=sub_query, content="", error=last_error,
                )
        if self.verbose:
            _log_result(result)
        return result

    # -- public API ----------------------------------------------------------

    async def astream(self, user_task: str):
        """Async event stream. Same event shapes as ``Supervisor.stream``.

        Concurrent-dispatch mode (``sequential=False``) yields
        ``subtask_result`` events as each sub-task finishes -- so the
        UI sees whichever completes first, not the plan order.
        """
        yield {"type": "plan_start"}
        plan = await self._plan(user_task)

        if not plan:
            if self.verbose:
                _log_no_plan()
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[], subtasks=[],
            )
            yield {"type": "final", "content": result.content}
            yield {"type": "completion", "result": result}
            return

        yield {"type": "plan", "plan": plan}
        if self.verbose:
            _log_plan(plan)

        # Emit a `dispatch` event for every step up-front so UIs can
        # render the plan as a "task list" before results start landing.
        for step_idx, item in enumerate(plan):
            yield {"type": "dispatch",
                   "agent": item.get("agent"), "query": item.get("query", ""),
                   "step": step_idx}

        # 3.3 unified completion-driven scheduler. Dependencies per step:
        #   - DAG mode (any step has depends_on): the parsed edges.
        #   - legacy sequential: every prior step (all-prior threading,
        #     identical context to pre-3.3) — with max_parallel=1 this
        #     reproduces the old sequential loop exactly.
        #   - legacy concurrent: no deps — everything runs at once, no
        #     threading, exactly the old asyncio.gather behavior.
        # Cascade + skip_when apply only in DAG mode (legacy plans never
        # skipped later steps on failure, and must not start now).
        dag_mode = _plan_uses_deps(plan)
        step_ids = [
            item.get("id") or f"step_{i + 1}" for i, item in enumerate(plan)
        ]
        if dag_mode:
            deps_of: List[List[str]] = [
                list(item.get("depends_on") or []) for item in plan
            ]
        elif self.sequential:
            deps_of = [step_ids[:i] for i in range(len(plan))]
        else:
            deps_of = [[] for _ in plan]

        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        done_ids: set = set()
        launched: set = set()
        running: Dict[asyncio.Task, int] = {}

        def _ready() -> List[int]:
            out = []
            for i in range(len(plan)):
                if i in launched:
                    continue
                if all(d in done_ids for d in deps_of[i] if d in step_ids):
                    out.append(i)
            return out

        def _record(i: int, r: SubtaskResult) -> SubtaskResult:
            r.step_id = step_ids[i]
            r.depends_on = deps_of[i]
            subtask_results.append(r)
            results_by_id[step_ids[i]] = r
            done_ids.add(step_ids[i])
            return r

        while len(done_ids) < len(plan):
            # Launch everything ready (respecting max_parallel), resolving
            # cascades and condition-skips inline — those complete
            # instantly without dispatching.
            progressed = False
            for i in _ready():
                if dag_mode:
                    failed_dep = next(
                        (d for d in deps_of[i]
                         if d in results_by_id and results_by_id[d].error),
                        None,
                    )
                    if failed_dep is not None:
                        launched.add(i)
                        r = _record(i, SubtaskResult(
                            agent=plan[i].get("agent", "<none>"),
                            query=plan[i].get("query", ""),
                            content="",
                            error=(f"skipped: dependency '{failed_dep}' failed "
                                   f"({results_by_id[failed_dep].error})"),
                            skipped=True,
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
                    skip, reason = _evaluate_skip_when(
                        plan[i].get("skip_when"), results_by_id,
                        deps_of[i], self.verbose,
                    )
                    if skip:
                        launched.add(i)
                        r = _record(i, SubtaskResult(
                            agent=plan[i].get("agent", "<none>"),
                            query=plan[i].get("query", ""),
                            content=f"skipped: {reason}",
                            skipped=True,
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
                if self.max_parallel is not None and len(running) >= self.max_parallel:
                    break
                launched.add(i)
                dep_results = [
                    results_by_id[d] for d in deps_of[i] if d in results_by_id
                ]
                task = asyncio.create_task(self._run_subtask(
                    plan[i]["agent"], plan[i]["query"],
                    prior_results=dep_results if dep_results else None,
                ))
                running[task] = i
                progressed = True

            if progressed and len(done_ids) >= len(plan):
                break
            if not running:
                if not progressed:
                    # Nothing running, nothing launchable: only possible if
                    # a dep id points at a step outside the plan (sanitizer
                    # prevents this) — bail rather than spin.
                    break
                continue

            done, _ = await asyncio.wait(
                running.keys(), return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                i = running.pop(t)
                r = _record(i, t.result())
                yield {"type": "subtask_result", "result": r,
                       "step": i, "step_id": step_ids[i]}

        yield {"type": "synthesize_start"}
        final = await self._synthesize(user_task, list(subtask_results))
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
        )
        yield {"type": "final", "content": final}
        yield {"type": "completion", "result": result}

    async def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks concurrently, and synthesize.

        Thin wrapper over ``astream()`` -- consumes every event and
        returns the final ``SupervisorResult``. Use ``astream()``
        directly if you want to render progress in a UI.
        """
        result: Optional[SupervisorResult] = None
        async for event in self.astream(user_task):
            if event["type"] == "completion":
                result = event["result"]
        assert result is not None, "AsyncSupervisor.astream must yield completion"
        return result

    def __repr__(self) -> str:
        return (
            f"<AsyncSupervisor(agents={list(self.agents.keys())}, "
            f"max_subtasks={self.max_subtasks})>"
        )
