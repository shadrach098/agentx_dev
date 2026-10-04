"""Persistent runs: keep an agent working through errors, inside a budget.

Design: docs/superpowers/specs/2026-10-04-persistent-agents-design.md

Everything here is opt-in. ``AgentRunner(persistence=Persistence(...))`` and
``Supervisor(persistence=Persistence(...))`` switch it on; with
``persistence=None`` (the default) none of this code runs.
"""

from __future__ import annotations

import asyncio
import hashlib
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


def _obs_hash(text: Any) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


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

    def record(self, name: str, args: Any, result: Any, is_error: bool, rung: int = 0) -> None:
        line = f"{name}({_short_args(args)}) -> {_first_line(result)}"
        if is_error:
            self.failed.append(f"{line} [rung {rung}]")
            del self.failed[: -self.max_entries]
        else:
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
            f"Done ({len(self.done)} calls, last 10 shown):\n{done}\n"
            f"Failed ({len(self.failed)} calls):\n{self.render_failed()}\n"
            f"Next: {self.next or '(not stated)'}"
        )


# ---------------------------------------------------------------------------
# Stuck detection and the reflection ladder
# ---------------------------------------------------------------------------

class StuckTracker:
    """Counts consecutive bad turns. A stuck signal fires when any streak
    reaches ``reflect_after``; a successful call that is neither a repeat nor
    a repeated result is progress and clears every streak."""

    def __init__(self, reflect_after: int):
        self.n = reflect_after
        self._last_sig: Optional[str] = None
        self._last_obs: Optional[str] = None
        self.reset()

    def reset(self) -> None:
        self.same = 0
        self.errors = 0
        self.same_obs = 0

    def observe(self, sig: str, obs: str, is_error: bool) -> Tuple[Optional[str], bool]:
        """Returns ``(reason, progressed)``; ``reason`` is None unless a streak fired."""
        same_sig = sig == self._last_sig
        same_obs = obs == self._last_obs
        self._last_sig, self._last_obs = sig, obs
        self.same = self.same + 1 if same_sig else 1
        self.same_obs = self.same_obs + 1 if same_obs else 1
        self.errors = self.errors + 1 if is_error else 0
        if (not is_error) and (not same_sig) and (not same_obs):
            # Progress. This call is occurrence #1 of its own streak, which
            # keeps "3 identical calls" meaning three calls, as before.
            self.same = self.same_obs = 1
            self.errors = 0
            return None, True
        if self.errors >= self.n:
            return f"{self.errors} tool errors in a row", False
        if self.same >= self.n:
            return f"the same call repeated {self.same} times", False
        if self.same_obs >= self.n:
            return f"{self.same_obs} identical results in a row", False
        return None, False


REFLECTION_RUNGS: Tuple[str, ...] = (
    "State the root cause of the failure in one sentence, then do NOT repeat the call that failed.",
    "Choose an approach you have NOT tried yet. These attempts already failed:\n{failed}",
    "Before another write or retry, check your assumptions with one cheap read-only probe "
    "(list the directory, print the value, read the file).",
    "If you are still blocked, say exactly what is blocking you and what you would need to "
    "continue, and give that as your final answer.",
)


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
    return out + [{"type": "text", "text": block.lstrip()}]


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
