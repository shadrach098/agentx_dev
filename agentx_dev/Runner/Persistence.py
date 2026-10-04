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
