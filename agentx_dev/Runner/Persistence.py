"""Persistent runs: keep an agent working through errors, inside a budget.

Design: docs/superpowers/specs/2026-10-04-persistent-agents-design.md

Everything here is opt-in. ``AgentRunner(persistence=Persistence(...))`` and
``Supervisor(persistence=Persistence(...))`` switch it on; with
``persistence=None`` (the default) none of this code runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from agentx_dev.ChatModel import CostBudgetExceeded

logger = logging.getLogger(__name__)

OUTCOME_DONE = "done"
OUTCOME_STUCK = "stuck"
OUTCOME_OUT_OF_TIME = "out_of_time"
OUTCOME_OUT_OF_BUDGET = "out_of_budget"
OUTCOME_ITERATION_LIMIT = "iteration_limit"
OUTCOME_PARTIAL = "partial"   # Supervisor only


@dataclass(frozen=True)
class Persistence:
    """Opt-in settings for runs that keep working through errors.

    ``max_minutes`` is the wall-clock limit for the whole run (a Supervisor
    and its specialists share it). The cost limit is not a field here: it is
    the model's own cost cap (``model.configure_limits(budget_usd=...)``).

    ``reflect_after`` consecutive failing or repeating turns force a
    reflection; the reflection ladder has ``max_reflections`` rungs, and if
    the stuck signal fires again after the last rung the run ends ``stuck``.
    History is compacted once it passes ``compact_at_tokens``, keeping the
    last ``keep_recent_turns`` messages verbatim. ``max_replans`` bounds
    Supervisor recovery rounds per stuck episode. ``max_turns`` is a backstop
    on total model turns. ``patient_retries`` keeps retrying transient
    provider errors (429, 5xx, timeouts) with backoff until the deadline.
    """

    max_minutes: float = 30.0
    reflect_after: int = 3
    max_reflections: int = 4
    compact_at_tokens: int = 60_000
    keep_recent_turns: int = 6
    max_replans: int = 3
    max_turns: int = 1000
    patient_retries: bool = True

    def __post_init__(self) -> None:
        if not self.max_minutes > 0:
            raise ValueError(f"Persistence.max_minutes must be > 0, got {self.max_minutes!r}")
        for name, minimum in (
            ("reflect_after", 2), ("max_reflections", 1), ("compact_at_tokens", 1),
            ("keep_recent_turns", 2), ("max_replans", 0), ("max_turns", 1),
        ):
            value = getattr(self, name)
            if value < minimum:
                raise ValueError(f"Persistence.{name} must be >= {minimum}, got {value!r}")


class BudgetExpired(Exception):
    """The run's wall-clock limit was reached."""


class RunStuck(Exception):
    """The reflection ladder was exhausted; the message is the stuck reason."""


class RunBudget:
    """A deadline on a monotonic clock, shared by a run and everything under it."""

    def __init__(self, deadline: float, clock: Optional[Callable[[], float]] = None):
        self.deadline = deadline
        self._clock = clock or time.monotonic

    @classmethod
    def start(cls, minutes: float, clock: Optional[Callable[[], float]] = None) -> "RunBudget":
        clock = clock or time.monotonic
        return cls(clock() + minutes * 60.0, clock)

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise BudgetExpired("time limit reached")

    def capped(self, minutes: float) -> "RunBudget":
        """A budget that ends at the earlier of this deadline and ``minutes`` from now."""
        return RunBudget(min(self.deadline, self._clock() + minutes * 60.0), self._clock)


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _clip(text: Any, n: int) -> str:
    s = str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_line(text: Any, n: int = 120) -> str:
    lines = str(text).strip().splitlines()
    return _clip(lines[0] if lines else "", n)


def _short_args(args: Any, n: int = 80) -> str:
    try:
        s = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        s = repr(args)
    return _clip(s, n)


def _signature(name: str, args: Any) -> str:
    try:
        return f"{name}::{json.dumps(args, sort_keys=True, default=repr)}"
    except Exception:
        return f"{name}::{args!r}"


# ---------------------------------------------------------------------------
# Progress ledger (kept by the framework, not the model)
# ---------------------------------------------------------------------------

@dataclass
class ProgressLedger:
    """What the run has done, what failed, and what the model said it would do
    next. Free (no model call) and cannot drift."""

    goal: str = ""
    done: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    next: str = ""
    max_entries: int = 40
    done_count: int = 0       # totals; the lists above keep only the last ``max_entries``
    failed_count: int = 0

    def record(self, name: str, args: Any, result: Any, is_error: bool, rung: int = 0) -> None:
        line = f"{name}({_short_args(args)}) -> {_first_line(result)}"
        if is_error:
            self.failed_count += 1
            self.failed.append(f"{line} [rung {rung}]")
            del self.failed[: -self.max_entries]
        else:
            self.done_count += 1
            self.done.append(line)
            del self.done[: -self.max_entries]

    def note_plan(self, thought: Any) -> None:
        self.next = _clip(thought, 300)

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "done": list(self.done),
                "failed": list(self.failed), "next": self.next}

    def render_failed(self) -> str:
        if not self.failed:
            return "  (none)"
        return "\n".join(f"  - {x}" for x in self.failed)

    def render(self) -> str:
        done = "\n".join(f"  - {x}" for x in self.done[-10:]) or "  (nothing yet)"
        return (
            f"Goal: {_clip(self.goal, 300)}\n"
            f"Done ({self.done_count} calls, last 10 shown):\n{done}\n"
            f"Failed ({self.failed_count} calls):\n{self.render_failed()}\n"
            f"Next: {self.next or '(not stated)'}"
        )


# ---------------------------------------------------------------------------
# Stuck detection and the reflection ladder
# ---------------------------------------------------------------------------

class StuckTracker:
    """Counts consecutive bad turns. A stuck signal fires when the same call
    repeats ``reflect_after`` times or ``reflect_after`` tool errors come in a
    row; a successful call that is not a repeat is progress and clears both
    streaks. Identical *results* are deliberately not a signal: constant
    success strings ("ok") are normal for write/delete tools in a healthy run."""

    def __init__(self, reflect_after: int):
        self.n = reflect_after
        self._last_sig: Optional[str] = None
        self._last_turn: Optional[str] = None
        self.reset()

    def reset(self) -> None:
        self.same = 0
        self.errors = 0
        self.same_turns = 0

    def observe(self, sig: str, is_error: bool) -> Tuple[Optional[str], bool]:
        """Returns ``(reason, progressed)``; ``reason`` is None unless a streak fired."""
        same_sig = sig == self._last_sig
        self._last_sig = sig
        self.same = self.same + 1 if same_sig else 1
        self.errors = self.errors + 1 if is_error else 0
        if (not is_error) and (not same_sig):
            # Progress. This call is occurrence #1 of its own streak, which
            # keeps "3 identical calls" meaning three calls, as before.
            self.same = 1
            self.errors = 0
            return None, True
        if self.errors >= self.n:
            return f"{self.errors} tool errors in a row", False
        if self.same >= self.n:
            return f"the same call repeated {self.same} times", False
        return None, False

    def observe_turn(self, turn_sig: Optional[str]) -> Tuple[Optional[str], bool]:
        """Turn-level check for multi-call batches, which the per-call streaks
        cannot see: in ``[read(a), read(b)]`` every call differs from the one
        before it. Pass the batch's signature, or None for a single-call turn
        (that clears the turn streak). Returns ``(reason, repeated)``:
        ``repeated`` means the batch equals the previous turn's, so it is not
        progress."""
        repeated = turn_sig is not None and turn_sig == self._last_turn
        self._last_turn = turn_sig
        if turn_sig is None:
            self.same_turns = 0
            return None, False
        self.same_turns = self.same_turns + 1 if repeated else 1
        if self.same_turns >= self.n:
            return f"the same batch of calls repeated {self.same_turns} times", True
        return None, repeated


REFLECTION_RUNGS: Tuple[str, ...] = (
    "State the root cause of the failure in one sentence, then do NOT repeat the call that failed.",
    "Choose an approach you have NOT tried yet. These attempts already failed:\n{failed}",
    "Before another write or retry, check your assumptions with one cheap read-only probe "
    "(list the directory, print the value, read the file).",
    "If you are still blocked, say exactly what is blocking you and what you would need to "
    "continue, and give that as your final answer.",
)


def _turn_signature(calls: List[Tuple[str, Any, str, bool]]) -> str:
    """Order-independent signature of a whole turn (sorted name + args)."""
    return json.dumps(sorted(_signature(name, args) for name, args, _, _ in calls))


def reflection_message(rung: int, reason: str, failed_text: str) -> str:
    """The text appended to the last observation when a stuck signal fires.
    Rungs past the last reuse the last one."""
    body = REFLECTION_RUNGS[min(rung, len(REFLECTION_RUNGS)) - 1].format(failed=failed_text)
    return f"\n\n[framework] You appear to be stuck ({reason}); recovery step {rung}. {body}"


# ---------------------------------------------------------------------------
# History compaction
# ---------------------------------------------------------------------------

NOTES_MARK = "[Progress notes -- earlier turns were compacted]"

NOTES_PROMPT = (
    "You are compacting the working notes of an autonomous agent that is partway through a task.\n"
    "Write notes the agent will rely on to continue without the earlier turns: facts and values it "
    "learned, file paths and identifiers, what is already done, and what remains. Do not include "
    "failed attempts (they are tracked separately). Be concrete and keep it under 400 words.\n\n"
    "{prior}EARLIER TURNS TO COMPACT:\n{middle}"
)


def estimate_tokens(history: List[Dict[str, Any]]) -> int:
    """Cheap size estimate (no tokenizer): characters / 4, with each media part
    counted as a flat 1,500 tokens."""
    chars = 0
    media = 0
    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    chars += len(str(p))
                    continue
                kind = p.get("type")
                src = p.get("source") or {}
                if kind == "text":
                    chars += len(str(p.get("text", "")))
                elif kind == "document" and src.get("type") == "text":
                    chars += len(str(src.get("data", "")))
                else:
                    media += 1
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"], default=str))
    return chars // 4 + media * 1500


def _render_message(m: Dict[str, Any]) -> str:
    role = m.get("role", "?")
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(p.get("text", "[media]")) if isinstance(p, dict) else str(p) for p in c)
    lines = [f"{role}: {_clip(c or '', 1500)}"]
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        lines.append(f"  -> {fn.get('name')}({_clip(fn.get('arguments', ''), 300)})")
    return "\n".join(lines)


def render_for_summary(middle: List[Dict[str, Any]], prior_notes: str = "", cap: int = 40_000) -> str:
    rendered = "\n".join(_render_message(m) for m in middle)
    if len(rendered) > cap:
        rendered = "[... oldest turns dropped ...]\n" + rendered[-cap:]
    prior = f"NOTES SO FAR:\n{prior_notes}\n\n" if prior_notes else ""
    return NOTES_PROMPT.format(prior=prior, middle=rendered)


_NOTES_SEP = "\n\n" + NOTES_MARK + "\n"


def _split_notes(content: Any) -> Tuple[Any, str]:
    """``(content_without_notes, prior_notes_text)`` for a task message's content."""
    if isinstance(content, str):
        base, _, notes = content.partition(_NOTES_SEP)
        return base, notes
    if isinstance(content, list):
        out: List[Any] = []
        notes = ""
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and NOTES_MARK in str(p.get("text", "")):
                base, _, notes = str(p["text"]).partition(_NOTES_SEP)
                out.append({**p, "text": base})
            else:
                out.append(p)
        return out, notes
    return content, ""


def _with_notes(content: Any, notes: str) -> Any:
    block = _NOTES_SEP + notes
    if isinstance(content, str):
        return content + block
    out = list(content)
    for i, p in enumerate(out):
        if isinstance(p, dict) and p.get("type") == "text":
            out[i] = {**p, "text": str(p.get("text", "")) + block}
            return out
    return out + [{"type": "text", "text": block}]


def plan_compaction(
    history: List[Dict[str, Any]], task_index: int, keep_recent: int,
) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
    """Decide what to compact. Returns ``(tail_start, middle_messages)`` or
    None when there is nothing worth compacting.

    The kept tail starts at an ``assistant`` message: the task message before
    it stays a user turn (valid alternation for both providers), and an
    assistant turn's tool results follow it, so a native tool call is never
    separated from its result."""
    n = len(history)
    tail = max(task_index + 1, n - keep_recent)
    while tail > task_index + 1 and (tail >= n or history[tail].get("role") != "assistant"):
        tail -= 1
    if tail >= n or history[tail].get("role") != "assistant":
        return None
    middle = history[task_index + 1: tail]
    if len(middle) < 2:
        return None
    return tail, middle


def apply_compaction(history: List[Dict[str, Any]], task_index: int, tail: int, notes: str) -> None:
    """Replace ``history[task_index+1:tail]`` with ``notes`` attached to the task message (in place)."""
    msg = history[task_index]
    base, _ = _split_notes(msg.get("content"))
    history[task_index] = {**msg, "content": _with_notes(base, notes)}
    del history[task_index + 1: tail]


def build_notes(summary: Optional[str], prior: str, failed_text: str) -> str:
    """The notes block: the model's summary (or, failing that, the previous
    notes) plus the ledger's failed attempts, verbatim."""
    body = (summary or "").strip()
    if not body:
        body = prior.split("\n\nFailed attempts so far:")[0].strip()
    if not body:
        body = "(earlier turns were compacted; no summary was available)"
    return f"{body}\n\nFailed attempts so far:\n{failed_text or '  (none)'}"


def compact_history(
    history: List[Dict[str, Any]],
    *,
    task_index: int,
    keep_recent: int,
    summarize: Callable[[str], str],
    failed_text: str = "",
) -> bool:
    """Plan, summarize, and apply one compaction. Returns False when there was
    nothing to compact. A failing ``summarize`` falls back to the ledger alone."""
    plan = plan_compaction(history, task_index, keep_recent)
    if plan is None:
        return False
    tail, middle = plan
    _, prior = _split_notes(history[task_index].get("content"))
    summary: Optional[str] = None
    try:
        summary = summarize(render_for_summary(middle, prior))
    except Exception as e:
        logger.warning("compaction summary failed (%s); using the ledger alone", e)
    apply_compaction(history, task_index, tail, build_notes(summary, prior, failed_text))
    return True


# ---------------------------------------------------------------------------
# Transient errors, budget plumbing, runner configuration helpers
# ---------------------------------------------------------------------------

_TRANSIENT_NAME = re.compile(r"Timeout|Connection|RateLimit|Overloaded|ServiceUnavailable|InternalServer")


def is_transient(exc: BaseException) -> bool:
    """True for errors worth waiting out: HTTP 408/429/5xx, timeouts,
    connection errors, rate-limit and overloaded errors. Everything else
    (auth, invalid request, programming errors) is not transient."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return bool(_TRANSIENT_NAME.search(type(exc).__name__))


def accepts_budget(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` can be called with the internal ``_budget=`` keyword."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "_budget" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@contextmanager
def apply_persistence(runners: Iterable[Any], persistence: Optional[Persistence]):
    """Give each runner that has a ``persistence`` attribute set to None the
    supervisor's settings for the duration of the block, then restore it.
    Runners with their own settings, and objects without the attribute, are untouched."""
    touched: List[Any] = []
    if persistence is not None:
        for r in runners:
            if hasattr(r, "persistence") and getattr(r, "persistence") is None:
                r.persistence = persistence
                touched.append(r)
    try:
        yield
    finally:
        for r in touched:
            r.persistence = None


class PersistenceMixin:
    """Gives a runner a validated ``persistence`` property.

    While persistence is set:

    - the runner's ``max_iterations`` is ``persistence.max_turns`` (persistent
      runs are bounded by the deadline and the budget, not a small step count);
    - the runner's tool-result cache is suspended, because a retry answered
      from the cache is not a retry and a cached "write succeeded" can be
      false (spec 4.9).

    Clearing it restores both. This is what lets a Supervisor switch
    persistence on for runners that were built without it. Runners must have
    set ``max_iterations`` and built ``self.registry`` before the first
    assignment."""

    _persistence: Optional[Persistence] = None
    _base_max_iterations: int = 4
    _saved_cache: Tuple[Any, Any] = (None, None)

    @property
    def persistence(self) -> Optional[Persistence]:
        return self._persistence

    @persistence.setter
    def persistence(self, value: Optional[Persistence]) -> None:
        if value is not None and not isinstance(value, Persistence):
            raise TypeError(f"persistence= must be a Persistence instance, got {type(value).__name__}")
        was = self._persistence
        registry = getattr(self, "registry", None)
        if value is not None and was is None:
            self._base_max_iterations = self.max_iterations
            if registry is not None:
                self._saved_cache = (getattr(registry, "cache", None), getattr(registry, "_cache_ttl", None))
                registry.configure_cache(None, None)
        elif value is None and was is not None:
            self.max_iterations = self._base_max_iterations
            if registry is not None:
                registry.configure_cache(*self._saved_cache)
        self._persistence = value
        if value is not None:
            self.max_iterations = value.max_turns


def _append_to_last(history: List[Dict[str, Any]], text: str) -> None:
    """Attach framework text to the last message (an observation), so no
    extra turn is added and role alternation stays valid."""
    last = history[-1]
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = c + text
    elif isinstance(c, list):
        last["content"] = list(c) + [{"type": "text", "text": text.lstrip()}]
    else:
        last["content"] = (str(c) if c else "") + text


# ---------------------------------------------------------------------------
# The per-run state the loops talk to
# ---------------------------------------------------------------------------

class PersistentRun:
    """State and hooks for one persistent run: the deadline, the ledger, the
    stuck tracker, compaction, and patient retries. The runner loops call:

    - ``before_turn`` / ``abefore_turn``: before each model call;
    - ``patient`` / ``apatient``: around each model call;
    - ``after_turn``: after each batch of tool results;
    - ``bind``: once, to hand over the history lists;
    - ``drain``: to collect the events to stream.
    """

    def __init__(
        self,
        cfg: Persistence,
        goal: str,
        *,
        budget: Optional[RunBudget] = None,
        verbose: bool = False,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        asleep: Optional[Callable[[float], Any]] = None,
    ):
        self.cfg = cfg
        self.verbose = verbose
        clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._asleep = asleep or asyncio.sleep
        self.budget = (
            budget.capped(cfg.max_minutes) if budget is not None
            else RunBudget.start(cfg.max_minutes, clock)
        )
        self.ledger = ProgressLedger(goal=goal)
        self.tracker = StuckTracker(cfg.reflect_after)
        self.rung = 0
        self.outcome = OUTCOME_DONE
        self.detail = ""
        self.headline = ""
        self.working_history: List[Dict[str, Any]] = []
        self.tool_calls: List[Any] = []
        self.steps: List[str] = []
        self.task_index = 0
        self._events: List[Dict[str, Any]] = []
        self._floor = 0
        self.agent_event: Any = None   # observability AGENT_START event, set by the sync loop

    # -- wiring ------------------------------------------------------------

    def bind(self, working_history, tool_calls, steps, task_index: int) -> None:
        """Hand over the loop's own lists (they are mutated in place by the
        loop, and compaction edits ``working_history`` in place)."""
        self.working_history = working_history
        self.tool_calls = tool_calls
        self.steps = steps
        self.task_index = task_index

    def emit(self, event: Dict[str, Any]) -> None:
        self._events.append(event)

    def drain(self) -> List[Dict[str, Any]]:
        events, self._events = self._events, []
        return events

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"\x1B[1;33m[persist] {text}\x1B[0m")
        else:
            logger.info("persist: %s", text)

    # -- hook: before each model call ---------------------------------------

    def _compaction_job(self):
        before = estimate_tokens(self.working_history)
        if before <= max(self.cfg.compact_at_tokens, self._floor + self.cfg.compact_at_tokens // 4):
            return None
        plan = plan_compaction(self.working_history, self.task_index, self.cfg.keep_recent_turns)
        if plan is None:
            return None
        tail, middle = plan
        _, prior = _split_notes(self.working_history[self.task_index].get("content"))
        return before, tail, middle, prior

    def _finish_compaction(self, before: int, tail: int, summary: Optional[str], prior: str) -> None:
        apply_compaction(self.working_history, self.task_index, tail,
                         build_notes(summary, prior, self.ledger.render_failed()))
        after = estimate_tokens(self.working_history)
        self._floor = after
        self.emit({"type": "compact", "before_tokens": before, "after_tokens": after})
        self._say(f"compacted history: ~{before} -> ~{after} tokens")

    def before_turn(self, model: Any) -> None:
        """Raises ``BudgetExpired`` past the deadline; compacts an oversized history."""
        self.budget.check()
        job = self._compaction_job()
        if job is None:
            return
        before, tail, middle, prior = job
        summary: Optional[str] = None
        try:
            summary = self.patient(lambda: model.Initialize(
                messages=[{"role": "user", "content": render_for_summary(middle, prior)}]))
        except (CostBudgetExceeded, BudgetExpired):
            raise
        except Exception as e:
            logger.warning("compaction summary failed (%s); using the ledger alone", e)
        self._finish_compaction(before, tail, summary, prior)

    async def abefore_turn(self, model: Any) -> None:
        """Async twin of :meth:`before_turn` (the summary call is awaited)."""
        self.budget.check()
        job = self._compaction_job()
        if job is None:
            return
        before, tail, middle, prior = job
        summary: Optional[str] = None
        try:
            summary = await self.apatient(lambda: model.async_initialize(
                messages=[{"role": "user", "content": render_for_summary(middle, prior)}]))
        except (CostBudgetExceeded, BudgetExpired):
            raise
        except Exception as e:
            logger.warning("compaction summary failed (%s); using the ledger alone", e)
        self._finish_compaction(before, tail, summary, prior)

    # -- hook: around each model call ---------------------------------------

    def _backoff(self, exc: BaseException, attempt: int) -> float:
        """Seconds to wait before retrying ``exc``; re-raises when it must not be retried."""
        if not (self.cfg.patient_retries and is_transient(exc)):
            raise exc
        wait = min(60.0, 2.0 ** attempt)
        if self.budget.remaining() <= wait:
            raise BudgetExpired("time limit reached while retrying a provider error") from exc
        self._say(f"transient provider error ({exc}); retrying in {wait:g}s")
        return wait

    def patient(self, fn: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as e:
                wait = self._backoff(e, attempt)
            self._sleep(wait)
            attempt += 1

    async def apatient(self, factory: Callable[[], Any]) -> Any:
        """``factory`` is a zero-argument callable returning an awaitable."""
        attempt = 0
        while True:
            try:
                return await factory()
            except Exception as e:
                wait = self._backoff(e, attempt)
            await self._asleep(wait)
            attempt += 1

    # -- hook: after each batch of tool results ----------------------------

    def note_thought(self, thought: Any) -> None:
        if thought:
            self.ledger.note_plan(thought)

    def after_turn(self, history: List[Dict[str, Any]], calls: List[Tuple[str, Any, str, bool]]) -> None:
        """Feed the ledger and the stuck tracker. On a stuck signal, append the
        next rung's recovery message to the last observation, or raise
        ``RunStuck`` when the ladder is exhausted."""
        reason: Optional[str] = None
        progressed = False
        for name, args, text, is_error in calls:
            self.ledger.record(name, args, text, is_error, self.rung)
            r, p = self.tracker.observe(_signature(name, args), is_error)
            reason = reason or r
            progressed = progressed or p
        # A batch of calls is progress only if it differs from the previous turn;
        # single-call turns are judged by the per-call streaks above alone.
        turn_reason, repeated = self.tracker.observe_turn(
            _turn_signature(calls) if len(calls) > 1 else None)
        if repeated:
            progressed = False
        reason = reason or turn_reason
        if reason is None:
            if progressed:
                self.rung = 0
            return
        self.rung += 1
        if self.rung > self.cfg.max_reflections:
            raise RunStuck(reason)
        self.tracker.reset()
        _append_to_last(history, reflection_message(self.rung, reason, self.ledger.render_failed()))
        self.emit({"type": "reflect", "rung": self.rung, "reason": reason})
        self._say(f"reflect: recovery step {self.rung} ({reason})")

    # -- exit ---------------------------------------------------------------

    def finish(self, outcome: str, detail: str = "", headline: str = "") -> None:
        """Record how the run ended. ``headline`` replaces the outcome's
        standard first line of the report (for exits the ladder did not cause)."""
        self.outcome = outcome
        self.detail = detail
        self.headline = headline

    def report(self) -> str:
        head = self.headline or {
            OUTCOME_STUCK: f"Stopped: stuck after {self.cfg.max_reflections} recovery attempts ({self.detail}).",
            OUTCOME_OUT_OF_TIME: f"Stopped: the {self.cfg.max_minutes:g}-minute time limit was reached.",
            OUTCOME_OUT_OF_BUDGET: f"Stopped: the cost budget was reached ({self.detail}).",
            OUTCOME_ITERATION_LIMIT: f"Stopped: the {self.cfg.max_turns}-turn limit was reached.",
        }.get(self.outcome, "Stopped.")
        return f"{head}\n\n{self.ledger.render()}"

    def exit_completion(self, model_name: str, user_input: str):
        from agentx_dev.Agents import AgentCompletion
        return AgentCompletion.from_agent(
            model_name=model_name, query=user_input, content=self.report(),
            tool_calls=list(self.tool_calls), steps=list(self.steps),
            history=self.working_history, outcome=self.outcome,
            progress=self.ledger.to_dict(),
        )

    def exit_events(self, model_name: str, user_input: str):
        completion = self.exit_completion(model_name, user_input)
        yield {"type": "final", "content": completion.content}
        yield {"type": "completion", "completion": completion}

# ---------------------------------------------------------------------------
# Entry points the runners call
# ---------------------------------------------------------------------------

def unrecognized_action_headline(action: Any) -> str:
    """Report headline for a persistent run whose model emitted an action that
    is neither a tool nor a final answer, with no answer text to fall back on."""
    shown = (str(action or "") or "<empty>")[:60]
    return (f"Stopped: stuck: the agent emitted an unrecognized action ({shown!r}) "
            f"and no answer text.")


def _remember(runner: Any, user_input: str, content: str) -> None:
    """Mirror the runner's normal end-of-run memory write for early exits."""
    if getattr(runner, "auto_memory", False) and getattr(runner, "_memory", None):
        runner._memory.add_message("user", user_input)
        runner._memory.add_message("assistant", content)


def _end_agent_event(state: "PersistentRun", content: str) -> None:
    """End the observability AGENT_START event a loop left open when an early exit
    (deadline, exhausted ladder, cost limit) unwound it before its own end_event."""
    if state.agent_event is None:
        return
    from agentx_dev.Observability import observability
    observability.end_event(state.agent_event, data={
        "final_answer": str(content)[:100],
        "iterations": len(state.steps),
        "tool_calls": len(state.tool_calls),
        "outcome": state.outcome,
    })


def run_persistent(runner, user_input, chat_history, stream_tokens, media, budget):
    """Generator wrapper for ``AgentRunner._iter_run``. Runs the loop under a
    ``PersistentRun`` and turns the exceptions that legitimately end a
    persistent run (deadline, exhausted ladder, cost limit) into a normal
    ``completion`` event whose ``outcome`` says why. Anything else
    (authentication errors, programming errors) propagates."""
    state = PersistentRun(runner.persistence, user_input, budget=budget, verbose=runner.verbose)
    try:
        yield from runner._iter_run_core(user_input, chat_history, stream_tokens, media, state)
        return
    except BudgetExpired:
        state.finish(OUTCOME_OUT_OF_TIME)
    except RunStuck as e:
        state.finish(OUTCOME_STUCK, str(e))
    except CostBudgetExceeded as e:
        state.finish(OUTCOME_OUT_OF_BUDGET, str(e))
    yield from state.drain()
    events = list(state.exit_events(runner.model.__class__.__name__, user_input))
    content = events[-1]["completion"].content
    _end_agent_event(state, content)
    _remember(runner, user_input, content)
    yield from events


async def run_persistent_async(runner, user_input, budget, run):
    """Async twin of :func:`run_persistent`. ``run`` is ``lambda state: <coroutine>``
    that runs the loop with the given ``PersistentRun``."""
    state = PersistentRun(runner.persistence, user_input, budget=budget, verbose=runner.verbose)
    try:
        return await run(state)
    except BudgetExpired:
        state.finish(OUTCOME_OUT_OF_TIME)
    except RunStuck as e:
        state.finish(OUTCOME_STUCK, str(e))
    except CostBudgetExceeded as e:
        state.finish(OUTCOME_OUT_OF_BUDGET, str(e))
    completion = state.exit_completion(runner.model.__class__.__name__, user_input)
    _end_agent_event(state, completion.content)
    _remember(runner, user_input, completion.content)
    return completion
