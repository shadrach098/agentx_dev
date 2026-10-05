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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union, Any

from pydantic import BaseModel, Field

from agentx_dev.ChatModel import BaseChatModel, CostBudgetExceeded
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.Tools import logger
from agentx_dev.SubAgents import (   # noqa: F401  (SpawnConfig/SpawnRequest are re-exported)
    DELEGATE_TOOL_NAME, AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpawnRequest, SpecError,
    _default_interactive_approver, attach_delegation, parse_agent_spec, spawn_instruction,
    spec_from_legacy_spawn,
)
from agentx_dev.Runner.Persistence import (
    OUTCOME_DONE, OUTCOME_OUT_OF_BUDGET, OUTCOME_OUT_OF_TIME, OUTCOME_PARTIAL, OUTCOME_STUCK,
    BudgetExpired, Persistence, PersistentRun, RunBudget, _clip, accepts_budget,
    apply_persistence, budget_event,
)


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
    # How the specialist's run ended: "done", "stuck", "iteration_limit",
    # "out_of_time", "out_of_budget". Anything but "done" is a failed attempt.
    outcome: str = "done"
    # Persistent specialists attach their progress ledger (goal/done/failed/next).
    progress: Optional[Dict[str, Any]] = None
    # Set when a recovery step that "replaces" this failed step finished "done";
    # superseded failures no longer count.
    superseded: bool = False

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


@contextmanager
def _per_run_agents(sup):
    """Give one run its own copy of the specialist registry, so agents spawned during
    the run are discarded when it ends and ``sup.agents`` is exactly as it was."""
    base = sup.agents
    sup.agents = dict(base)
    try:
        yield
    finally:
        sup.agents = base


def _step_agent_name(item: dict) -> Optional[str]:
    """The agent a plan step runs on: its ``agent``, or the name its ``new_agent`` defines."""
    name = item.get("agent")
    if name:
        return name
    spec = item.get("new_agent")
    return spec.get("name") if isinstance(spec, dict) else None


class SupervisorResult(BaseModel):
    """Aggregate result produced by a Supervisor run."""

    query: str
    content: str  # final synthesized answer
    subtasks: List[SubtaskResult] = Field(default_factory=list)
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan
    # "done" | "partial" | "stuck" | "out_of_time" | "out_of_budget"
    outcome: str = "done"
    # Sub-agents created during the run (planner-time spawns and delegations):
    # name, origin, tools, dropped, outcome, chars. Empty when nothing was spawned.
    spawned: List[Dict[str, Any]] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

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


def _outcome_of(completion: Any) -> str:
    """The completion's ``outcome``; completions from custom runners that don't set one are "done"."""
    value = getattr(completion, "outcome", OUTCOME_DONE)
    return value if isinstance(value, str) else OUTCOME_DONE


def _progress_of(completion: Any) -> Optional[dict]:
    value = getattr(completion, "progress", None)
    return value if isinstance(value, dict) else None


def _unfinished_reason(outcome: str) -> str:
    return f"the specialist ended with outcome '{outcome}' instead of finishing"


def _result_failed(r: "SubtaskResult") -> bool:
    """A real step that did not finish: it errored, or its specialist ended
    with an outcome other than "done". Spawn bookkeeping and steps already
    replaced by a recovery round do not count."""
    return r.agent != "__spawn__" and not r.superseded and (bool(r.error) or r.outcome != OUTCOME_DONE)


def _result_done(r: "SubtaskResult") -> bool:
    return r.agent != "__spawn__" and not r.skipped and not r.superseded and not _result_failed(r)


def _supervisor_outcome(results: List["SubtaskResult"], budget_reason: Optional[str] = None) -> str:
    """``done`` when nothing is unresolved, ``partial`` when some steps
    finished and some did not, ``stuck`` when none finished. A spent budget
    (``budget_reason``) names why the unresolved work was left."""
    if not any(_result_failed(r) for r in results):
        return OUTCOME_DONE
    if budget_reason:
        return budget_reason
    return OUTCOME_PARTIAL if any(_result_done(r) for r in results) else OUTCOME_STUCK


def _budget_reason(budget: Optional[RunBudget], results: List["SubtaskResult"]) -> Optional[str]:
    """Why the run must stop trying: a specialist hit the cost cap, or time is up."""
    if any(r.outcome == OUTCOME_OUT_OF_BUDGET for r in results):
        return OUTCOME_OUT_OF_BUDGET
    if (budget is not None and budget.expired()) or any(r.outcome == OUTCOME_OUT_OF_TIME for r in results):
        return OUTCOME_OUT_OF_TIME
    return None


def _resolve_replaced(recovery: List[dict], results: List["SubtaskResult"]) -> int:
    """After a recovery round: mark each unresolved step that a recovery step
    ``replaces`` as superseded, but only when that recovery step finished
    ``done``. Steps nothing replaced, or whose replacement failed, stay
    unresolved. Returns how many steps were resolved (the round's progress)."""
    by_id = {r.step_id: r for r in results if r.step_id and r.agent != "__spawn__"}
    resolved = 0
    for step in recovery:
        new = by_id.get(step.get("id"))
        if new is None or not _result_done(new):
            continue
        for old_id in step.get("replaces") or []:
            old = by_id.get(old_id)
            if old is not None and _result_failed(old):
                old.superseded = True
                resolved += 1
    return resolved


def _recovery_note(results: List["SubtaskResult"], round_no: int) -> str:
    """Text appended to the planner prompt for a recovery round: what is kept,
    what did not finish and why, and what was already tried."""
    done = [r for r in results if _result_done(r) and r.step_id]
    failed = [r for r in results if _result_failed(r)]
    lines = [
        "", "",
        f"── RECOVERY ROUND {round_no} ──",
        "Some steps of the earlier plan did not finish. Do NOT repeat the approach that failed.",
        "Produce a NEW plan, in the same JSON format, containing ONLY the work that is still needed.",
        "",
    ]
    if done:
        lines.append("Completed steps (their results are kept; do not redo them; "
                     "list their ids in depends_on to use their output):")
        lines += [f"  - [{r.step_id}] {r.agent}: {_clip(r.content, 300)}" for r in done]
        lines.append("")
    lines.append("Steps that did NOT finish:")
    for r in failed:
        why = r.error or f"ended with outcome '{r.outcome}'"
        lines.append(f"  - [{r.step_id}] {r.agent}: {_clip(r.query, 200)}")
        lines.append(f"      why: {_clip(why, 300)}")
        tried = (r.progress or {}).get("failed") or []
        if tried:
            lines.append("      attempts that failed:")
            lines += [f"        * {t}" for t in tried[-5:]]
    lines += ["", "Give every new step a NEW id. Pick a different specialist or method, or spawn one if the "
                  "catalog cannot do it. Only reference completed step ids in depends_on.",
              "When a new step redoes the work of a step that did not finish, list that step's id in the "
              "new step's \"replaces\" field, for example \"replaces\": [\"step_2\"]. A step that did not "
              "finish stays unresolved (and the task cannot be reported complete) unless a step that "
              "replaces it finishes."]
    return "\n".join(lines)


def _rename_colliding_ids(plan: List[dict], prior_ids: Iterable[str], round_no: int) -> List[dict]:
    """Ids defined by a recovery plan shadow earlier ones: a step whose id was
    already used gets ``r<round>_<id>``, and references to it inside the plan
    follow. References to earlier steps the plan does not redefine are kept."""
    prior = set(prior_ids)
    mapping = {s["id"]: f"r{round_no}_{s['id']}" for s in plan if s["id"] in prior}
    if not mapping:
        return plan
    out: List[dict] = []
    for step in plan:
        s = dict(step)
        s["id"] = mapping.get(s["id"], s["id"])
        if s.get("depends_on"):
            s["depends_on"] = [mapping.get(d, d) for d in s["depends_on"]]
        out.append(s)
    return out


def _unresolved_note(unresolved: List["SubtaskResult"]) -> str:
    lines = ["", "",
             "IMPORTANT: the following steps did NOT finish. State plainly which parts of the task were "
             "not completed and why. Never present data from them as established, and never claim the "
             "task is complete:"]
    lines += [f"- [{r.step_id}] {r.agent}: {r.error or r.outcome}" for r in unresolved]
    return "\n".join(lines)


def _log_replan(round_no: int, unresolved: List["SubtaskResult"]) -> None:
    ids = ", ".join(str(r.step_id) for r in unresolved)
    print(f"{_C_PLAN}[supervisor.replan] round {round_no}: recovering {ids}{_C_RESET}")


def _log_budget(event: Dict[str, Any]) -> None:
    print(f"{_C_ERROR}[supervisor.budget] the {event['reason']} limit ended the run{_C_RESET}")


def _budget_outcome_of(exc: BaseException) -> str:
    """``out_of_budget`` for a cost-cap error, ``out_of_time`` for a deadline."""
    return OUTCOME_OUT_OF_BUDGET if isinstance(exc, CostBudgetExceeded) else OUTCOME_OUT_OF_TIME


def _stopped_before_planning(user_task: str, exc: BaseException) -> "SupervisorResult":
    """Persistent mode: the budget ran out while the first plan was being made."""
    outcome = _budget_outcome_of(exc)
    what = "cost budget" if outcome == OUTCOME_OUT_OF_BUDGET else "time limit"
    return SupervisorResult(query=user_task, content=f"Stopped: the {what} was reached before a plan was made.",
                            plan=[], subtasks=[], outcome=outcome)


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
        "contain no cycles. A new_agent needs a name (letters, digits, "
        "_ or -, at most 40 characters), non-empty instructions, tools as "
        "a list of names, and must not be combined with agent."
    )


def _plan_uses_deps(plan: List[dict]) -> bool:
    """True when ANY step declares ``depends_on`` — the DAG-mode switch.
    A dep-free plan keeps byte-identical legacy semantics."""
    return any(
        isinstance(step, dict) and step.get("depends_on")
        for step in plan
    )


def _sanitize_plan(
    plan: List[dict], verbose: bool = False, known_ids: Iterable[str] = (),
    replaceable: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], List[str]]:
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
      6. (recovery plans, when ``replaceable`` is given) ``replaces`` keeps
         only ids of steps that are currently unresolved; anything else is
         dropped, and an empty or malformed ``replaces`` is removed

    ``known_ids`` are step ids finished in earlier recovery rounds; a
    dependency on one is valid and kept (it is simply not an edge in this
    plan's graph).
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

    # -- 7. new_agent validation (3.6) ----------------------------------------
    # A valid definition is normalized and also names the step's agent; an invalid one is
    # removed, and a step left with nothing to run is dropped (dependents lose the edge below).
    checked: List[dict] = []
    for step in plan:
        if "new_agent" not in step:
            checked.append(step)
            continue
        raw = step.pop("new_agent")
        problem = ""
        spec = None
        try:
            spec = parse_agent_spec(raw)
        except SpecError as e:
            problem = str(e)
        if spec is not None and step.get("agent") not in (None, "", spec.name):
            problem = f"a step cannot have both agent={step.get('agent')!r} and new_agent"
        if problem:
            repairs.append(f"step {step['id']!r}: new_agent invalid ({problem}) -- dropped")
            if not step.get("agent") or step.get("agent") == "__spawn__":
                continue
            checked.append(step)
            continue
        step["new_agent"] = {"name": spec.name, "instructions": spec.instructions, "tools": list(spec.tools)}
        step["agent"] = spec.name
        checked.append(step)
    plan = checked

    ids = [s["id"] for s in plan]
    id_pos = {sid: i for i, sid in enumerate(ids)}
    spawn_ids = {s["id"] for s in plan if s.get("agent") == "__spawn__"}

    known = set(known_ids)

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
            elif d not in id_pos and d not in known:
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
                if d not in id_pos:      # an earlier round's step: not part of this graph
                    continue
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

    # -- 6. replaces (recovery plans only) ----------------------------------
    if replaceable is not None:
        allowed = set(replaceable)
        for step in plan:
            if "replaces" not in step:
                continue
            raw = step.pop("replaces")
            if not isinstance(raw, list):
                repairs.append(f"step {step['id']!r}: replaces was not a list -- dropped")
                continue
            kept: List[str] = []
            for rid in raw:
                if rid in allowed:
                    if rid not in kept:
                        kept.append(rid)
                else:
                    repairs.append(f"step {step['id']!r}: replaces unknown or finished step {rid!r} -- dropped")
            if kept:
                step["replaces"] = kept

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
    # Dependencies outside this plan (a finished step from an earlier recovery
    # round) are already satisfied and are not part of this graph.
    indeg = [sum(1 for d in (s.get("depends_on") or []) if d in id_idx) for s in plan]
    dependents: List[List[int]] = [[] for _ in plan]
    for i, step in enumerate(plan):
        for d in step.get("depends_on") or []:
            if d in id_idx:
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

class _SpawnMixin:
    """Sub-agent creation shared by ``Supervisor`` and ``AsyncSupervisor`` (3.6). Needs
    ``self.agents``, ``self.model``, ``self.persistence``, ``self.spawn_config``,
    ``self.verbose`` and ``self._spawn_run_policy``."""

    _SPAWN_ASYNC = False        # AsyncSupervisor builds async sub-agents

    def _new_spawn_policy(self) -> SpawnPolicy:
        return SpawnPolicy(self.spawn_config, self.model, persistence=self.persistence,
                           is_async=self._SPAWN_ASYNC, verbose=self.verbose)

    def _spawn_policy(self) -> SpawnPolicy:
        """The policy for the run in progress (created on demand outside a run)."""
        if self._spawn_run_policy is None:
            self._spawn_run_policy = self._new_spawn_policy()
        return self._spawn_run_policy

    def _drain_spawn_events(self):
        policy = self._spawn_run_policy
        if policy is not None:
            yield from policy.run.drain()

    def _overlapping(self, built) -> Optional[str]:
        """Name of a registered specialist that already has every tool ``built`` has (log only)."""
        have = {getattr(t, "name", None) for t in getattr(built.runner, "tools", [])}
        have -= {DELEGATE_TOOL_NAME, None}
        if not have:
            return None
        for n, (_d, runner) in self.agents.items():
            if n == built.spec.name:
                continue
            if have <= {getattr(t, "name", None) for t in getattr(runner, "tools", [])}:
                return n
        return None

    def _spawn_spec(self, spec: AgentSpec) -> Tuple[Optional[str], str]:
        """Build, reuse or refuse ``spec``. Returns ``(name, "")`` when an agent is available
        under ``name`` (a clashing name may have been suffixed), or ``(None, reason)``."""
        policy = self._spawn_policy()
        registered = {n for n in self.agents if n not in policy.run.built}
        try:
            built = policy.obtain(spec, registered=registered)
        except SpawnRefused as e:
            return None, e.reason
        name = built.spec.name
        if built.reused != "registered" and name not in self.agents:
            self.agents[name] = Specialist(description=built.description, runner=built.runner)
        if built.reused is None and self.verbose:
            twin = self._overlapping(built)
            if twin:
                print(f"{_C_PLAN}[supervisor.spawn] '{name}' has the same tools as the registered "
                      f"specialist '{twin}'; keeping both (different instructions){_C_RESET}")
        return name, ""

    def _handle_spawn(self, spawn_step: dict) -> Tuple[Optional[str], Optional[str]]:
        """Process a legacy ``__spawn__`` step. Returns ``(name, None)`` when the agent is
        available and ``(None, None)`` otherwise. The 3.5 reroute is gone: a spawn whose
        tools overlap an existing specialist is no longer refused."""
        try:
            spec = spec_from_legacy_spawn(spawn_step)
        except SpecError as e:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] malformed request: {e}{_C_RESET}")
            return None, None
        name, reason = self._spawn_spec(spec)
        if name is None and self.verbose:
            print(f"{_C_ERROR}[supervisor.spawn] refused: {reason}{_C_RESET}")
        return name, None


class Supervisor(_SpawnMixin):
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
        persistence: Optional[Persistence] = None,
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
                auto_spawn / approver knobs. (3.6) The planner can define a new
                specialist inline (``new_agent``: name, instructions, tools) and
                specialists can ``delegate`` to fresh sub-agents; tools are clipped
                to the config's ceiling (``tools`` / ``capabilities``). With
                ``persistence`` set and no ``spawn_config`` the default is
                ``SpawnConfig(enabled=True, capabilities={"web", "files_read"},
                max_spawns=6)``; pass ``SpawnConfig(enabled=False)`` to opt out.
            max_subtask_retries: How many times a sub-task that RAISES (or
                returns an outcome other than "done") is re-dispatched
                before the Supervisor gives up on it.
                Default 1 (so a specialist that hits a transient or
                self-correctable failure — a malformed tool call, a bad
                escape — gets a second chance instead of the whole
                sub-task being abandoned on the first error). Each retry
                appends the prior error to the query so the specialist
                knows what to fix. Set to 0 to restore the old
                quit-on-first-failure behavior. Errors and non-"done"
                outcomes trigger a retry (stopping early once the shared
                budget is spent); a sub-task that returns "done" content
                (even thin content) is accepted as-is — unless you also
                pass ``subtask_success_check`` (below).
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
            persistence: (3.5) Opt-in ``Persistence(...)``. When a step does
                not finish, the Supervisor replans around it (completed
                steps are kept), bounded by ``max_replans`` per stuck
                episode and by one shared ``max_minutes`` deadline that
                every specialist inherits. It is applied to specialist
                runners that have none, for the duration of the run.
                ``None`` (default) keeps the one-shot plan/run/synthesize
                behaviour.
        """
        self.model = model
        # Normalize (and copy) so run-time spawns don't mutate the
        # caller's dict. Accepts (description, runner) tuples or
        # Specialist entries (3.3); everything is stored as Specialist,
        # which still tuple-unpacks for older code.
        self.agents = _normalize_agents(agents)
        self.max_subtasks = max_subtasks
        self.verbose = verbose
        if spawn_config is None:
            # Persistent supervisors may create sub-agents out of the box, inside a safe
            # ceiling (web search plus read-only files). Without persistence the default is off.
            spawn_config = (
                SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)
                if persistence is not None else SpawnConfig(enabled=False)
            )
        self.spawn_config = spawn_config
        self._spawn_run_policy: Optional[SpawnPolicy] = None
        self.max_subtask_retries = max(0, int(max_subtask_retries))
        self.subtask_success_check = subtask_success_check
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
        # Persistent runs only: waits out transient planner/synthesis errors until the deadline.
        self._patience: Optional[PersistentRun] = None

    # -- internal helpers ----------------------------------------------------

    def _call_model(self, messages: List[Dict[str, str]]) -> str:
        """One planner or synthesis call. In persistent mode a transient provider
        error (429, 5xx, timeout) is retried with backoff until the deadline."""
        patience = getattr(self, "_patience", None)
        if patience is None:
            return self.model.Initialize(messages=messages)
        return patience.patient(lambda: self.model.Initialize(messages=messages))

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
            prompt = prompt + spawn_instruction(self._spawn_policy())
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = self._call_model(messages)

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

    def _plan_recovery(self, user_task: str, results: List[SubtaskResult], round_no: int) -> List[dict]:
        """Ask the planner for a recovery plan covering only the unfinished
        work. Returns ``[]`` when it cannot (the caller then synthesizes with
        what it has). A spent budget propagates so the caller can say so."""
        try:
            raw = self._plan_once(user_task, repair_note=_recovery_note(results, round_no))
        except (CostBudgetExceeded, BudgetExpired):
            raise
        except Exception as e:
            logger.warning(f"recovery planning failed: {e}")
            return []
        if not raw:
            return []
        known = {r.step_id for r in results
                 if r.step_id and r.agent != "__spawn__" and not _result_failed(r) and not r.superseded}
        replaceable = {r.step_id for r in results if r.step_id and _result_failed(r)}
        sane, _ = _sanitize_plan(raw, verbose=self.verbose, known_ids=known, replaceable=replaceable)
        prior = {r.step_id for r in results if r.step_id}
        return _rename_colliding_ids(sane, prior, round_no)

    def _dispatch_with_retry(
        self,
        agent_runner: AgentRunner,
        agent_name: str,
        sub_query: str,
        dispatched_query: str,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
        """Run one sub-task, retrying up to ``max_subtask_retries`` times.

        Three kinds of failure are retried, each feeding context back into
        the query:
          - a RAISED exception (crash, unrecoverable tool error),
          - a returned completion whose ``outcome`` is not "done" (the
            specialist gave up: stuck, out of time, out of budget,
            iteration limit), with or without a success check, and
          - a returned "done" result that ``subtask_success_check``
            rejects (ran fine but produced nothing useful).

        Without a success check, a "done" result is accepted even if thin,
        since the Supervisor can't tell "terse but correct" from "wrong"
        on its own.

        The retry loop stops early when the shared run budget is spent (a
        result with outcome ``out_of_time`` or ``out_of_budget``): another
        attempt cannot help. ``budget`` is the shared ``RunBudget`` handed
        to specialists that accept one (persistent mode); ``None`` otherwise.

        On exhaustion: returns the last result with ``error`` set (content
        preserved) if we got one, else an empty errored SubtaskResult."""
        attempts = self.max_subtask_retries + 1
        query = dispatched_query
        last_error = ""
        last_result: Optional[SubtaskResult] = None
        for attempt in range(1, attempts + 1):
            try:
                if budget is not None and accepts_budget(agent_runner.Initialize):
                    completion = agent_runner.Initialize(query, _budget=budget)
                else:
                    completion = agent_runner.Initialize(query)
                result = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    # Preserve the runner's validated Pydantic instance so
                    # downstream steps get typed data, not just prose.
                    output=getattr(completion, "output", None),
                    outcome=_outcome_of(completion),
                    progress=_progress_of(completion),
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
            if result.outcome != OUTCOME_DONE:
                ok, reason = False, _unfinished_reason(result.outcome)
            else:
                ok, reason = self._evaluate_success(result)
            if ok:
                return result
            last_result = result
            last_error = f"did not meet success criteria: {reason}"
            if result.outcome in (OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET):
                break          # the shared budget is spent; another attempt cannot help
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

    def _synthesize(
        self,
        user_task: str,
        subtask_results: List[SubtaskResult],
        unresolved: Optional[List[SubtaskResult]] = None,
    ) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        if unresolved:
            prompt += _unresolved_note(unresolved)
        messages = [{"role": "user", "content": prompt}]
        try:
            return self._call_model(messages)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            what = "cost budget" if isinstance(e, CostBudgetExceeded) else "time limit"
            return f"Stopped: the {what} was reached.\n\n{results_block}"

    # -- public API ----------------------------------------------------------

    def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        spawn_rewrites: Dict[str, str],
        budget: Optional[RunBudget] = None,
    ):
        """Execute ``plan``: dispatch each step (spawning specialists, cascading
        failures, evaluating ``skip_when``) and yield ``dispatch`` / ``spawn`` /
        ``subtask_result`` events. Results are appended to the shared
        ``subtask_results`` / ``results_by_id`` so several rounds can build on
        one another."""
        # 3.3: DAG mode fires when ANY step declares depends_on. Dep-free
        # plans keep byte-identical legacy semantics (plan order, every
        # step sees ALL prior results).
        dag_mode = _plan_uses_deps(plan)
        order = _topo_order(plan) if dag_mode else list(range(len(plan)))

        for step_idx in order:
            item = plan[step_idx]
            agent_name = item.get("agent")
            step_id = item.get("id") or f"step_{step_idx + 1}"
            step_deps = list(item.get("depends_on") or [])

            if agent_name == "__spawn__":
                spawned_name, _ = self._handle_spawn(item)
                yield from self._drain_spawn_events()
                label = item.get("name", "?")
                if spawned_name:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {label}",
                        content=f"registered new specialist '{spawned_name}' with "
                                f"capabilities: {', '.join(item.get('capabilities', []))}",
                        step_id=step_id,
                    )
                else:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {label}",
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

            if item.get("new_agent"):
                try:
                    spawned_name, reason = self._spawn_spec(parse_agent_spec(item["new_agent"]))
                except SpecError as e:
                    spawned_name, reason = None, str(e)
                yield from self._drain_spawn_events()
                if spawned_name is None:
                    sub_result = SubtaskResult(
                        agent=agent_name or "<none>", query=item.get("query", ""), content="",
                        error=f"spawn refused: {reason}", step_id=step_id, depends_on=step_deps,
                    )
                    if self.verbose:
                        print(f"{_C_ERROR}[supervisor.spawn] step {step_id}: {reason}{_C_RESET}")
                    subtask_results.append(sub_result)
                    results_by_id[step_id] = sub_result
                    yield {"type": "subtask_result", "result": sub_result,
                           "step": step_idx, "step_id": step_id}
                    continue
                agent_name = spawned_name

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
            if budget is not None and budget.expired():
                sub_result = SubtaskResult(
                    agent=agent_name, query=sub_query, content="",
                    error="skipped: the time limit was reached",
                    outcome=OUTCOME_OUT_OF_TIME, skipped=True,
                    step_id=step_id, depends_on=step_deps,
                )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue
            yield {"type": "dispatch", "agent": agent_name, "query": sub_query,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_dispatch(agent_name, sub_query)
            sub_result = self._dispatch_with_retry(
                agent_runner, agent_name, sub_query, dispatched_query, budget,
            )
            sub_result.step_id = step_id
            sub_result.depends_on = step_deps
            subtask_results.append(sub_result)
            results_by_id[step_id] = sub_result
            policy = self._spawn_run_policy
            if policy is not None and agent_name in policy.run.built:
                policy.run.finish(agent_name, sub_result.outcome, len(sub_result.content))
            yield from self._drain_spawn_events()
            yield {"type": "subtask_result", "result": sub_result,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_result(sub_result)

    def stream(self, user_task: str):
        """Yield structured events as the supervisor plans, dispatches,
        and synthesizes.

        Event shapes:
          - {"type": "plan_start"}
          - {"type": "plan",           "plan": <list of steps>}
          - {"type": "spawn",          "name": str, "origin": "plan" | "delegate", "tools": list,
                                        "dropped": list, "reused": str | None, "refused": str | None,
                                        "capabilities": list, "rerouted_from": None}
          - {"type": "delegate_result", "name": str, "outcome": str, "chars": int}
          - {"type": "dispatch",       "agent": str, "query": str, "step": int}
          - {"type": "subtask_result", "result": SubtaskResult, "step": int}
          - {"type": "replan",         "round": int, "unresolved": list, "plan": list}  (persistent)
          - {"type": "budget",         "reason": "time" | "cost"}  (persistent; before synthesis)
          - {"type": "synthesize_start"}
          - {"type": "final",          "content": str}
          - {"type": "completion",     "result": SupervisorResult}   (last)

        The last event is always ``completion`` so ``run()`` can build
        on top of this and callers who want live UI updates can consume
        the intermediate events.
        """
        self._spawn_run_policy = self._new_spawn_policy()
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
        self._spawn_run_policy.budget = budget        # delegate falls back to it (see SpawnPolicy)
        self._patience = (PersistentRun(self.persistence, user_task, budget=budget, verbose=self.verbose)
                          if budget is not None else None)

        yield {"type": "plan_start"}
        try:
            plan = self._plan(user_task)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            stopped = _stopped_before_planning(user_task, e)
            spent = budget_event(stopped.outcome)
            yield spent
            if self.verbose:
                _log_budget(spent)
            yield {"type": "final", "content": stopped.content}
            yield {"type": "completion", "result": stopped}
            return

        if not plan:
            if self.verbose:
                _log_no_plan()
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
                outcome=OUTCOME_STUCK,
            )
            yield {"type": "final", "content": result.content}
            yield {"type": "completion", "result": result}
            return

        yield {"type": "plan", "plan": plan}
        if self.verbose:
            _log_plan(plan)

        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
        budget_reason: Optional[str] = None
        with _per_run_agents(self), \
                attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            yield from self._run_plan(plan, subtask_results, results_by_id, spawn_rewrites, budget)
            if self.persistence is not None:
                stagnant = 0
                round_no = 1
                while True:
                    unresolved = [r for r in subtask_results if _result_failed(r)]
                    if not unresolved:
                        break
                    budget_reason = _budget_reason(budget, subtask_results)
                    if budget_reason or stagnant >= self.persistence.max_replans:
                        break
                    try:
                        recovery = self._plan_recovery(user_task, subtask_results, round_no + 1)
                    except (CostBudgetExceeded, BudgetExpired) as e:
                        budget_reason = _budget_outcome_of(e)
                        break
                    if not recovery:
                        break
                    round_no += 1
                    yield {"type": "replan", "round": round_no,
                           "unresolved": [r.step_id for r in unresolved], "plan": recovery}
                    if self.verbose:
                        _log_replan(round_no, unresolved)
                    plan.extend(recovery)
                    yield from self._run_plan(recovery, subtask_results, results_by_id,
                                              spawn_rewrites, budget)
                    # Progress is an unresolved step resolved by a replacement that finished;
                    # a round that resolves nothing counts toward max_replans.
                    stagnant = 0 if _resolve_replaced(recovery, subtask_results) else stagnant + 1

        spent = budget_event(budget_reason)
        if spent is not None:
            yield spent
            if self.verbose:
                _log_budget(spent)
        yield {"type": "synthesize_start"}
        persistent = self.persistence is not None
        unresolved = [r for r in subtask_results if _result_failed(r)] if persistent else []
        shown = [r for r in subtask_results if not r.superseded] if persistent else subtask_results
        final = self._synthesize(user_task, shown, unresolved=unresolved)
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
            spawned=list(self._spawn_policy().run.records),
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

class AsyncSupervisor(_SpawnMixin):
    """
    Asynchronous multi-agent supervisor.

    Like :class:`Supervisor` but runs sub-tasks concurrently via
    ``asyncio.gather``. Accepts both :class:`AsyncAgentRunner` and
    :class:`AgentRunner` instances (sync runners are wrapped in
    ``asyncio.to_thread``).
    """

    _SPAWN_ASYNC = True

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
        persistence: Optional[Persistence] = None,
        spawn_config: Optional[SpawnConfig] = None,
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
            persistence: (3.5) Opt-in ``Persistence(...)``; see
                :class:`Supervisor`.
            spawn_config: (3.6) Lets the planner define new specialists inline
                (``new_agent``) and specialists ``delegate`` to fresh sub-agents,
                clipped to the config's ceiling. Spawned steps run in the same
                parallel batches as any other step. Same defaults as
                :class:`Supervisor` (on, with a safe ceiling, when ``persistence``
                is set and no config is given).
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
        self.persistence = persistence
        if spawn_config is None:
            # Same default as Supervisor: persistent runs may create sub-agents inside a safe ceiling.
            spawn_config = (
                SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)
                if persistence is not None else SpawnConfig(enabled=False)
            )
        self.spawn_config = spawn_config
        self._spawn_run_policy: Optional[SpawnPolicy] = None
        # Persistent runs only: waits out transient planner/synthesis errors until the deadline.
        self._patience: Optional[PersistentRun] = None

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
        """One planner or synthesis call. In persistent mode a transient provider
        error (429, 5xx, timeout) is retried with backoff until the deadline."""
        patience = getattr(self, "_patience", None)
        if patience is None:
            return await self._call_model_once(messages)
        return await patience.apatient(lambda: self._call_model_once(messages))

    async def _call_model_once(self, messages: List[Dict[str, str]]) -> str:
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
        if self.spawn_config.enabled:
            prompt = prompt + spawn_instruction(self._spawn_policy())
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
        # A step may name a registered agent, define one inline (new_agent), or use one an
        # earlier step defines; a legacy __spawn__ step carries no query.
        defined = {s["new_agent"].get("name") for s in plan
                   if isinstance(s, dict) and isinstance(s.get("new_agent"), dict)}
        defined |= {s.get("name") for s in plan
                    if isinstance(s, dict) and s.get("agent") == "__spawn__"}
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and (
                item.get("agent") == "__spawn__"
                or (item.get("query") and (
                    isinstance(item.get("new_agent"), dict)
                    or item.get("agent") in self.agents
                    or item.get("agent") in defined
                ))
            )
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

    async def _plan_recovery(self, user_task: str, results: List[SubtaskResult], round_no: int) -> List[dict]:
        """Async twin of :meth:`Supervisor._plan_recovery`."""
        try:
            raw = await self._plan_once(user_task, repair_note=_recovery_note(results, round_no))
        except (CostBudgetExceeded, BudgetExpired):
            raise
        except Exception as e:
            logger.warning(f"recovery planning failed: {e}")
            return []
        if not raw:
            return []
        known = {r.step_id for r in results
                 if r.step_id and r.agent != "__spawn__" and not _result_failed(r) and not r.superseded}
        replaceable = {r.step_id for r in results if r.step_id and _result_failed(r)}
        sane, _ = _sanitize_plan(raw, verbose=self.verbose, known_ids=known, replaceable=replaceable)
        prior = {r.step_id for r in results if r.step_id}
        return _rename_colliding_ids(sane, prior, round_no)

    async def _synthesize(
        self,
        user_task: str,
        subtask_results: List[SubtaskResult],
        unresolved: Optional[List[SubtaskResult]] = None,
    ) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        if unresolved:
            prompt += _unresolved_note(unresolved)
        messages = [{"role": "user", "content": prompt}]
        try:
            return await self._call_model(messages)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            what = "cost budget" if isinstance(e, CostBudgetExceeded) else "time limit"
            return f"Stopped: the {what} was reached.\n\n{results_block}"

    async def _run_subtask(
        self,
        agent_name: str,
        sub_query: str,
        prior_results: Optional[List[SubtaskResult]] = None,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
        """Run one sub-task, retrying up to ``max_subtask_retries`` times.

        Three kinds of failure are retried, each feeding context back into
        the query: a RAISED exception, a returned completion whose
        ``outcome`` is not "done" (with or without a success check), and a
        returned "done" result that ``subtask_success_check`` rejects. On
        exhaustion the last result is returned with ``error`` set (content
        preserved).

        The retry loop stops early when the shared run budget is spent (a
        result with outcome ``out_of_time`` or ``out_of_budget``): another
        attempt cannot help. ``budget`` is the shared ``RunBudget`` handed
        to specialists that accept one (persistent mode); ``None`` otherwise."""
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
                budget_kw = {"_budget": budget} if budget is not None and accepts_budget(initialize) else {}
                if asyncio.iscoroutinefunction(initialize):
                    completion = await initialize(query, **budget_kw)
                else:
                    completion = await asyncio.to_thread(initialize, query, **budget_kw)
                candidate = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    output=getattr(completion, "output", None),
                    outcome=_outcome_of(completion),
                    progress=_progress_of(completion),
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

            if candidate.outcome != OUTCOME_DONE:
                ok, reason = False, _unfinished_reason(candidate.outcome)
            else:
                ok, reason = self._evaluate_success(candidate)
            if ok:
                result = candidate
                break
            last_result = candidate
            last_error = f"did not meet success criteria: {reason}"
            if candidate.outcome in (OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET):
                break          # the shared budget is spent; another attempt cannot help
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

    async def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        budget: Optional[RunBudget] = None,
    ):
        """Execute ``plan`` with the completion-driven scheduler and yield
        ``dispatch`` / ``subtask_result`` events. Results are appended to the
        shared ``subtask_results`` / ``results_by_id`` so several rounds can
        build on one another."""
        # Emit a `dispatch` event for every step up-front so UIs can
        # render the plan as a "task list" before results start landing.
        for step_idx, item in enumerate(plan):
            if item.get("agent") == "__spawn__":
                continue                      # bookkeeping, not a dispatch
            yield {"type": "dispatch",
                   "agent": _step_agent_name(item), "query": item.get("query", ""),
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

        done_ids: set = set()
        launched: set = set()
        running: Dict[asyncio.Task, int] = {}
        launched_as: Dict[int, str] = {}      # step index -> the agent it was launched on

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

        # Every scheduled task is owned by this try/finally. Without it a
        # BaseException out of a specialist (asyncio.CancelledError and
        # KeyboardInterrupt are NOT Exception, so _run_subtask's handler
        # never sees them) propagated out of t.result() and left the
        # sibling tasks running unowned -- in-flight LLM calls still
        # billing with nobody collecting the result. The same finally
        # covers a consumer that stops iterating this generator early.
        try:
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
                    if budget is not None and budget.expired():
                        launched.add(i)
                        r = _record(i, SubtaskResult(
                            agent=plan[i].get("agent", "<none>"),
                            query=plan[i].get("query", ""),
                            content="",
                            error="skipped: the time limit was reached",
                            outcome=OUTCOME_OUT_OF_TIME,
                            skipped=True,
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
                    if self.max_parallel is not None and len(running) >= self.max_parallel:
                        break
                    launched.add(i)
                    if plan[i].get("agent") == "__spawn__":
                        spawned_name, _ = self._handle_spawn(plan[i])
                        for ev in self._drain_spawn_events():
                            yield ev
                        _record(i, SubtaskResult(
                            agent="__spawn__", query=f"spawn: {plan[i].get('name', '?')}",
                            content=(f"registered new specialist '{spawned_name}' with capabilities: "
                                     f"{', '.join(plan[i].get('capabilities', []))}") if spawned_name else "",
                            error=None if spawned_name else "spawn refused (see log for reason)",
                        ))
                        progressed = True
                        continue
                    launch_agent = plan[i].get("agent")
                    if plan[i].get("new_agent"):
                        try:
                            spawned_name, reason = self._spawn_spec(parse_agent_spec(plan[i]["new_agent"]))
                        except SpecError as e:
                            spawned_name, reason = None, str(e)
                        for ev in self._drain_spawn_events():
                            yield ev
                        if spawned_name is None:
                            r = _record(i, SubtaskResult(
                                agent=_step_agent_name(plan[i]) or "<none>", query=plan[i].get("query", ""),
                                content="", error=f"spawn refused: {reason}",
                            ))
                            yield {"type": "subtask_result", "result": r,
                                   "step": i, "step_id": step_ids[i]}
                            progressed = True
                            continue
                        launch_agent = spawned_name
                    if launch_agent not in self.agents:
                        r = _record(i, SubtaskResult(
                            agent=launch_agent or "<none>", query=plan[i].get("query", ""),
                            content="",
                            error=f"specialist '{launch_agent}' not in registry (spawn may have been refused)",
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
                    dep_results = [
                        results_by_id[d] for d in deps_of[i] if d in results_by_id
                    ]
                    task = asyncio.create_task(self._run_subtask(
                        launch_agent, plan[i]["query"],
                        prior_results=dep_results if dep_results else None,
                        budget=budget,
                    ))
                    running[task] = i
                    launched_as[i] = launch_agent
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
                    policy = self._spawn_run_policy
                    if policy is not None and launched_as.get(i) in policy.run.built:
                        policy.run.finish(launched_as[i], r.outcome, len(r.content))
                    for ev in self._drain_spawn_events():
                        yield ev
                    yield {"type": "subtask_result", "result": r,
                           "step": i, "step_id": step_ids[i]}
        finally:
            for _t in list(running):
                if not _t.done():
                    _t.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            running.clear()

    async def astream(self, user_task: str):
        """Async event stream. Same event shapes as ``Supervisor.stream``.

        Concurrent-dispatch mode (``sequential=False``) yields
        ``subtask_result`` events as each sub-task finishes -- so the
        UI sees whichever completes first, not the plan order.
        """
        self._spawn_run_policy = self._new_spawn_policy()
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
        self._spawn_run_policy.budget = budget        # delegate falls back to it (see SpawnPolicy)
        self._patience = (PersistentRun(self.persistence, user_task, budget=budget, verbose=self.verbose)
                          if budget is not None else None)
        yield {"type": "plan_start"}
        try:
            plan = await self._plan(user_task)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            stopped = _stopped_before_planning(user_task, e)
            spent = budget_event(stopped.outcome)
            yield spent
            if self.verbose:
                _log_budget(spent)
            yield {"type": "final", "content": stopped.content}
            yield {"type": "completion", "result": stopped}
            return

        if not plan:
            if self.verbose:
                _log_no_plan()
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[], subtasks=[],
                outcome=OUTCOME_STUCK,
            )
            yield {"type": "final", "content": result.content}
            yield {"type": "completion", "result": result}
            return

        yield {"type": "plan", "plan": plan}
        if self.verbose:
            _log_plan(plan)

        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        budget_reason: Optional[str] = None
        with _per_run_agents(self), \
                attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            plan_run = self._run_plan(plan, subtask_results, results_by_id, budget)
            try:
                async for event in plan_run:
                    yield event
            finally:
                await plan_run.aclose()
            if self.persistence is not None:
                stagnant = 0
                round_no = 1
                while True:
                    unresolved = [r for r in subtask_results if _result_failed(r)]
                    if not unresolved:
                        break
                    budget_reason = _budget_reason(budget, subtask_results)
                    if budget_reason or stagnant >= self.persistence.max_replans:
                        break
                    try:
                        recovery = await self._plan_recovery(user_task, subtask_results, round_no + 1)
                    except (CostBudgetExceeded, BudgetExpired) as e:
                        budget_reason = _budget_outcome_of(e)
                        break
                    if not recovery:
                        break
                    round_no += 1
                    yield {"type": "replan", "round": round_no,
                           "unresolved": [r.step_id for r in unresolved], "plan": recovery}
                    if self.verbose:
                        _log_replan(round_no, unresolved)
                    plan.extend(recovery)
                    plan_run = self._run_plan(recovery, subtask_results, results_by_id, budget)
                    try:
                        async for event in plan_run:
                            yield event
                    finally:
                        await plan_run.aclose()
                    stagnant = 0 if _resolve_replaced(recovery, subtask_results) else stagnant + 1

        spent = budget_event(budget_reason)
        if spent is not None:
            yield spent
            if self.verbose:
                _log_budget(spent)
        yield {"type": "synthesize_start"}
        persistent = self.persistence is not None
        unresolved = [r for r in subtask_results if _result_failed(r)] if persistent else []
        shown = [r for r in subtask_results if not r.superseded] if persistent else list(subtask_results)
        final = await self._synthesize(user_task, shown, unresolved=unresolved)
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
            spawned=list(self._spawn_policy().run.records),
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
