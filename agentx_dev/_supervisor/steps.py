"""Outcomes, sessions, recovery notes, the query a step is sent, and the verbose log lines."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


from agentx_dev.ChatModel import CostBudgetExceeded
from agentx_dev.Operator import (
    MAX_ANSWER_CHARS,
)
from agentx_dev.Runner.Persistence import (
    OUTCOME_DONE, OUTCOME_OUT_OF_BUDGET, OUTCOME_OUT_OF_TIME, OUTCOME_PARTIAL, OUTCOME_STUCK,
    RunBudget, _clip,
)
from agentx_dev._supervisor.results import (
    SubtaskResult, SupervisorResult,
)


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


SESSION_ANSWER_CHARS = 3000        # how much of an earlier round's answer a follow-up round is shown


def _session_settings(ask_user: Any, ask_when_done: bool, follow_up: bool, max_follow_ups: int):
    """``(follow_up, ask_when_done, max_follow_ups)`` for a constructor. ``follow_up`` implies
    ``ask_when_done``; either needs ``ask_user``."""
    follow = bool(follow_up)
    when_done = bool(ask_when_done) or follow
    if when_done and (ask_user is None or ask_user is False):
        raise ValueError("ask_when_done and follow_up need ask_user: pass ask_user=True or a function")
    return follow, when_done, max(0, int(max_follow_ups))


def _cap_text(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + " ..."


def _outcome_line(result: "SupervisorResult") -> str:
    """The sentence under the answer in a checkpoint prompt: how the round ended."""
    outcome = result.outcome
    if outcome == OUTCOME_OUT_OF_TIME:
        return f"Outcome: {outcome} (time limit reached)."
    if outcome == OUTCOME_OUT_OF_BUDGET:
        return f"Outcome: {outcome} (cost limit reached)."
    steps = [r for r in result.subtasks if r.agent != "__spawn__" and not r.superseded]
    failed = [r for r in steps if _result_failed(r)]
    if outcome in (OUTCOME_PARTIAL, OUTCOME_STUCK) and failed:
        return f"Outcome: {outcome} ({len(failed)} of {len(steps)} steps failed)."
    return f"Outcome: {outcome}."


def _follow_up_task(original: str, rounds: List[Dict[str, Any]], reply: str) -> str:
    """The task a follow-up round plans: the original, what each earlier round produced, and the
    operator's new instruction. The operator's earlier answers are not repeated here: the round's
    channel is seeded with them and the Supervisor appends its OPERATOR ANSWERS block."""
    lines = [original, "", "WHAT HAS HAPPENED SO FAR"]
    for r in rounds:
        asked = "" if r["round"] == 1 else f" (the operator asked: {_cap_text(r['query'], MAX_ANSWER_CHARS)})"
        lines += [f"Round {r['round']}{asked}, outcome {r['outcome']}:",
                  _cap_text(r["content"], SESSION_ANSWER_CHARS), ""]
    lines += [f"The operator now asks: {reply}",
              "Do this now, building on the results above. Do not repeat work that is already done."]
    return "\n".join(lines)


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


_RECAP_CHARS = 1500


def _augment_query_with_iteration_limit(base_query: str, recap: str, attempt: int) -> str:
    """The retry note for a specialist that ran out of steps (outcome ``iteration_limit``).

    The generic "change your METHOD" note sent such a helper to do the same research again with
    the same budget. What it needs to hear is that the steps were the problem and that nothing
    it found survived, so the note says that, shows a trimmed recap of what the last attempt
    did, and asks for an answer from what is already known."""
    recap = (recap or "").strip()
    if len(recap) > _RECAP_CHARS:
        recap = recap[:_RECAP_CHARS].rstrip() + " ..."
    shown = f"\n\nWhat it did:\n{recap}" if recap else ""
    return (
        f"{base_query}\n\n===\n\n[supervisor] Your PREVIOUS attempt (#{attempt}) ran out of "
        f"steps before it gave a Final_Answer, so everything it found was lost.{shown}\n\n"
        f"Make fewer lookups this time and stop researching as soon as you have enough. Answer "
        f"from what you can find quickly; an answer that says what is missing is better than "
        f"running out of steps again."
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
