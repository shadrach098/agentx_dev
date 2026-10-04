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
