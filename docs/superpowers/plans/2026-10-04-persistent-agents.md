# Persistent Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in persistent mode for `AgentRunner`, `AsyncAgentRunner`, `Supervisor` and `AsyncSupervisor`: keep working through errors until the task is done or a time/cost limit is reached, and report an honest outcome.

**Architecture:** One new module, `agentx_dev/Runner/Persistence.py`, holds all new logic (config, budget, progress ledger, stuck tracker, reflection ladder, history compaction, patient retries, and a per-run state object `PersistentRun`). The sync and async runner loops each call the same four hooks on that object. The Supervisors gain an outer recovery loop that replans unresolved steps under one shared deadline. Default mode (`persistence=None`) is unchanged except that every run now reports an `outcome` and the Supervisor treats a non-`done` outcome as a failed attempt.

**Tech Stack:** Python 3.9+, pydantic v2, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-persistent-agents-design.md` (read it first; this plan implements it section by section).

**Validation:** before handoff, the code and test steps of Tasks 1-12 were applied mechanically from this document to a scratch copy of the repo (anchors, snippets and tests exactly as written): 473 passed, 4 skipped (the original 309 plus 164 new). The documentation edits in Task 12 (README, cookbook, FAQ, troubleshooting, upgrading, changelog) and the Task 13 release checklist were not executed.

## Global Constraints

Every task's requirements implicitly include these. Values are copied from the spec.

- `Persistence` is a **frozen dataclass** in `agentx_dev/Runner/Persistence.py`, exported from the package root. Defaults: `max_minutes=30.0`, `reflect_after=3`, `max_reflections=4`, `compact_at_tokens=60_000`, `keep_recent_turns=6`, `max_replans=3`, `max_turns=1000`, `patient_retries=True`.
- `persistence=None` (the default) keeps today's behavior. The **only** change that affects agents which do not opt in is spec 4.1: every completion carries an `outcome`, and a Supervisor treats a returned `outcome != "done"` as a rejected attempt (retry via `max_subtask_retries`, then flag with `error`).
- Outcome strings: `done`, `stuck`, `out_of_time`, `out_of_budget`, `iteration_limit`, and (Supervisor only) `partial`.
- Reflection ladder has 4 rungs; if the stuck signal fires again after rung `max_reflections` the run ends `stuck`; any real progress resets counters and the ladder.
- Compaction invariants: never split a native tool-call from its result; valid role alternation for GPT-format and Claude-format histories; media in the original task message is untouched.
- A non-`done` outcome's `content` is a deterministic report built from the ledger (no extra model call).
- **Spec addendum 4.9 (from the external audit; needs the user's OK):** while `persistence` is set, a runner suspends its registry's tool-result cache and restores it when cleared. A retry answered from the cache is not a retry, and a cached "write succeeded" can be false. This does not fix the cache's cross-runner and side-effect hazards in default mode; that is a separate fix (spawned as its own task).
- New stream events (exact shapes): `{"type": "reflect", "rung": int, "reason": str}`, `{"type": "compact", "before_tokens": int, "after_tokens": int}`, and on supervisors `{"type": "replan", "round": int, "unresolved": [step ids], "plan": [...]}`. Existing event types are unchanged.
- No bare `git stash`. **Commit messages carry NO `Co-Authored-By` trailer** (standing user rule: the user is the sole contributor).
- Run tests with `python -m pytest <path> -q -p no:cacheprovider` from the worktree root. The existing suite (309 passed, 4 skipped at the start of this plan) must stay green after every task.
- Work on branch `feat/persistent-agents` (the spec is already committed there). Files use LF line endings.
- Every behavior is implemented for **both** the sync and async runner/supervisor, and tested on both.
- Version target is **3.5.0**; releasing is a separate step the user must approve (Task 13 is a checklist only).

## Coordination

- A separate session may be fixing the default tool-result cache (cross-runner leak, replayed writes), which also touches `AgentRun.py` and `AsyncAgentRun.py`. This plan does not depend on that fix: while `persistence` is set the cache is suspended (spec 4.9). Expect small, mechanical merge conflicts near `registry.configure_cache`; resolve them by keeping both changes.
- Tasks are ordered so each leaves the full suite green. Do not reorder them: Tasks 5-7 edit code the later tasks' anchors assume, and Task 9 must precede Tasks 10-11.

---

## File Structure

| File | Responsibility |
|---|---|
| `agentx_dev/Runner/Persistence.py` (new) | Everything new: `Persistence`, outcome constants, `BudgetExpired`/`RunStuck`, `RunBudget`, `ProgressLedger`, `StuckTracker`, reflection messages, compaction functions, `is_transient`, `accepts_budget`, `apply_persistence`, `PersistentRun`, `run_persistent`, `run_persistent_async`. |
| `agentx_dev/Agents/Agent.py` | `AgentCompletion.outcome`, `.progress`; `from_agent` passes them through. |
| `agentx_dev/Runner/AgentRun.py` | `persistence=` parameter; `_iter_run` becomes a thin wrapper around `_iter_run_core`; hooks; `outcome` on every exit path. |
| `agentx_dev/Runner/AsyncAgentRun.py` | Same for the async runner (`Initialize` wraps `_initialize_core`). |
| `agentx_dev/Supervisor.py` | `persistence=` on both supervisors; outcome fields; non-`done` is a rejected attempt; `_run_plan` extraction; recovery rounds; shared budget. |
| `agentx_dev/__init__.py` | Export `Persistence`. |
| `tests/test_persistence_core.py`, `test_persistence_tracker.py`, `test_persistence_compaction.py`, `test_persistence_run.py` | Unit tests for the module. |
| `tests/test_run_outcomes.py`, `test_persistent_runner.py`, `test_persistent_runner_async.py` | Runner behavior. |
| `tests/test_supervisor_outcomes.py`, `test_supervisor_persistent.py`, `test_supervisor_persistent_async.py` | Supervisor behavior. |
| `tests/test_persistent_soak.py` | Long-run context-bound check. |
| `docs/guides/long-running-agents.md` (new), README, cookbook, FAQ, troubleshooting, upgrading, CHANGELOG, `host/build_data.py`, `host/app.js`, `pyproject.toml` | Docs and the 3.5.0 version. |

---

### Task 1: Core types, budget, and completion fields

**Files:**
- Create: `agentx_dev/Runner/Persistence.py`
- Modify: `agentx_dev/Agents/Agent.py:491-527` (`AgentCompletion`)
- Modify: `agentx_dev/__init__.py` (export)
- Modify: `docs/superpowers/specs/2026-10-04-persistent-agents-design.md:208-210` (transient-error wording)
- Test: `tests/test_persistence_core.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (later tasks rely on these exact names):
  - constants `OUTCOME_DONE="done"`, `OUTCOME_STUCK="stuck"`, `OUTCOME_OUT_OF_TIME="out_of_time"`, `OUTCOME_OUT_OF_BUDGET="out_of_budget"`, `OUTCOME_ITERATION_LIMIT="iteration_limit"`, `OUTCOME_PARTIAL="partial"`
  - `Persistence(max_minutes, reflect_after, max_reflections, compact_at_tokens, keep_recent_turns, max_replans, max_turns, patient_retries)`
  - `class BudgetExpired(Exception)`, `class RunStuck(Exception)`
  - `RunBudget(deadline: float, clock=None)` with `.start(minutes, clock=None)` (classmethod), `.remaining() -> float`, `.expired() -> bool`, `.check() -> None` (raises `BudgetExpired`), `.capped(minutes) -> RunBudget`
  - `AgentCompletion.outcome: str = "done"`, `AgentCompletion.progress: Optional[Dict[str, Any]] = None`; `AgentCompletion.from_agent(..., outcome="done", progress=None)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistence_core.py`:

```python
import dataclasses

import pytest

from agentx_dev import Persistence
from agentx_dev.Agents import AgentCompletion
from agentx_dev.Runner.Persistence import (
    BudgetExpired, OUTCOME_DONE, OUTCOME_ITERATION_LIMIT, OUTCOME_OUT_OF_BUDGET,
    OUTCOME_OUT_OF_TIME, OUTCOME_PARTIAL, OUTCOME_STUCK, RunBudget, RunStuck,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_defaults_match_the_spec():
    p = Persistence()
    assert (p.max_minutes, p.reflect_after, p.max_reflections, p.compact_at_tokens,
            p.keep_recent_turns, p.max_replans, p.max_turns, p.patient_retries) == (
        30.0, 3, 4, 60_000, 6, 3, 1000, True)


def test_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Persistence().max_minutes = 1


@pytest.mark.parametrize("kwargs", [
    {"max_minutes": 0}, {"max_minutes": -1}, {"reflect_after": 1},
    {"max_reflections": 0}, {"compact_at_tokens": 0}, {"keep_recent_turns": 1},
    {"max_replans": -1}, {"max_turns": 0},
])
def test_rejects_nonsense_values(kwargs):
    with pytest.raises(ValueError, match="Persistence"):
        Persistence(**kwargs)


def test_zero_replans_is_allowed():
    assert Persistence(max_replans=0).max_replans == 0


def test_outcome_strings():
    assert (OUTCOME_DONE, OUTCOME_STUCK, OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET,
            OUTCOME_ITERATION_LIMIT, OUTCOME_PARTIAL) == (
        "done", "stuck", "out_of_time", "out_of_budget", "iteration_limit", "partial")


def test_exceptions_are_plain_exceptions():
    assert issubclass(BudgetExpired, Exception) and issubclass(RunStuck, Exception)


def test_budget_counts_down_and_expires():
    clock = Clock()
    b = RunBudget.start(1, clock)          # 1 minute
    assert b.remaining() == pytest.approx(60.0)
    assert not b.expired()
    b.check()                               # does not raise
    clock.t += 59
    assert not b.expired()
    clock.t += 2
    assert b.expired()
    with pytest.raises(BudgetExpired):
        b.check()


def test_capped_budget_never_outlives_its_parent():
    clock = Clock()
    parent = RunBudget.start(1, clock)
    assert parent.capped(10).deadline == parent.deadline       # parent is earlier
    assert parent.capped(0.5).deadline == pytest.approx(clock.t + 30)   # child is earlier


def test_completion_defaults_and_passthrough():
    c = AgentCompletion.from_agent(model_name="m", query="q", content="c",
                                   tool_calls=[], steps=[], history=[])
    assert c.outcome == "done" and c.progress is None
    c2 = AgentCompletion.from_agent(model_name="m", query="q", content="c", tool_calls=[],
                                    steps=[], history=[], outcome="stuck",
                                    progress={"goal": "g"})
    assert c2.outcome == "stuck" and c2.progress == {"goal": "g"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistence_core.py -q -p no:cacheprovider`
Expected: collection error / FAIL with `ImportError: cannot import name 'Persistence'`.

- [ ] **Step 3: Create `agentx_dev/Runner/Persistence.py`**

```python
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
```

- [ ] **Step 4: Add the completion fields**

In `agentx_dev/Agents/Agent.py`, in `class AgentCompletion`, add after the `output: Optional[Any] = None` line:

```python
    # How the run ended: "done" | "stuck" | "out_of_time" | "out_of_budget" |
    # "iteration_limit". Every run sets this; "done" means a final answer.
    outcome: str = "done"
    # Persistent runs attach the progress ledger as a plain dict
    # (goal / done / failed / next). None for ordinary runs.
    progress: Optional[Dict[str, Any]] = None
```

and change `from_agent` so the signature ends `..., history: List[Dict[str, str]], outcome: str = "done", progress: Optional[Dict[str, Any]] = None):` and the `cls(...)` call passes `outcome=outcome, progress=progress,` after `history=history`.

- [ ] **Step 5: Export `Persistence`**

In `agentx_dev/__init__.py`, after the line `from .Runner.AsyncAgentRun import AsyncAgentRunner` add:

```python
from .Runner.Persistence import Persistence
```

and in `__all__`, after `"AsyncAgentRunner",` add `"Persistence",`.

- [ ] **Step 6: Amend spec 4.6**

The implementation is stricter than the spec's parenthetical about what is "transient" (a bug in the framework must not be retried for half an hour). In `docs/superpowers/specs/2026-10-04-persistent-agents-design.md`, replace

```
With `patient_retries=True`, a transient error from the model call (the
existing `ChatModel._is_non_retryable` returns False for it: 408, 429,
5xx, timeouts, connection errors) is retried with exponential backoff
```

with

```
With `patient_retries=True`, a transient error from the model call (HTTP
408, 429 or 5xx; timeouts; connection errors; rate-limit and overloaded
errors. Anything else, including programming errors, raises immediately)
is retried with exponential backoff
```

In the same file, insert this section immediately before `## 5. Files`:

```
### 4.9 Tool-result cache (addendum, 2026-10-04)

The framework's default tool-result cache (`auto_cache=True`) is keyed on the tool function and its
arguments only. A cache hit returns before the tool runs, so a repeated call is not a retry, and a
cached "write succeeded" can be false. Persistent mode depends on retries and re-probing actually
executing, so a runner with `persistence` set suspends its registry's tool-result cache for as long
as persistence is set and restores it when cleared. This does not fix the cache's cross-runner and
side-effect hazards in default mode; those are tracked as a separate fix.
```

- [ ] **Step 7: Run the tests and the full suite**

Run: `python -m pytest tests/test_persistence_core.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: `309 passed, 4 skipped` plus the new tests, no failures.

- [ ] **Step 8: Commit**

```bash
git add agentx_dev/Runner/Persistence.py agentx_dev/Agents/Agent.py agentx_dev/__init__.py tests/test_persistence_core.py docs/superpowers/specs/2026-10-04-persistent-agents-design.md
git commit -m "feat(persistence): Persistence config, RunBudget, and completion outcome fields"
```

---

### Task 2: Progress ledger, stuck tracker, reflection ladder

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (append)
- Test: `tests/test_persistence_tracker.py`

**Interfaces:**
- Consumes: Task 1 imports already at the top of `Persistence.py` (`hashlib`, `json`, `dataclass`, `field`).
- Produces:
  - `_clip(text, n) -> str`, `_first_line(text, n=120) -> str`, `_short_args(args, n=80) -> str`, `_signature(name, args) -> str`
  - `ProgressLedger(goal="", done=[], failed=[], next="", max_entries=40)` with `.record(name, args, result, is_error, rung=0)`, `.note_plan(thought)`, `.to_dict() -> dict`, `.render_failed() -> str`, `.render() -> str`
  - `StuckTracker(reflect_after)` with `.observe(sig, is_error) -> (reason: Optional[str], progressed: bool)` and `.reset()`
  - `REFLECTION_RUNGS` (4-tuple of str), `reflection_message(rung, reason, failed_text) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistence_tracker.py`:

```python
from agentx_dev.Runner.Persistence import (
    ProgressLedger, REFLECTION_RUNGS, StuckTracker, _signature,
    reflection_message,
)


class TestLedger:
    def test_records_successes_and_failures(self):
        led = ProgressLedger(goal="fix the build")
        led.record("read_path", {"path": "a.py"}, "line one\nline two", False)
        led.record("run_python", {"code": "1/0"}, "ZeroDivisionError: division by zero", True, rung=2)
        assert led.done == ['read_path({"path": "a.py"}) -> line one']
        assert led.failed == ['run_python({"code": "1/0"}) -> ZeroDivisionError: division by zero [rung 2]']

    def test_entries_are_capped(self):
        led = ProgressLedger(max_entries=3)
        for i in range(10):
            led.record("t", {"i": i}, f"r{i}", False)
        assert len(led.done) == 3 and led.done[-1].endswith("r9")

    def test_render_has_every_section(self):
        led = ProgressLedger(goal="g")
        led.record("t", {}, "ok", False)
        led.record("u", {}, "boom", True)
        led.note_plan("try the other file")
        text = led.render()
        for needle in ("Goal: g", "Done (1 calls", "Failed (1 calls", "u({}) -> boom", "Next: try the other file"):
            assert needle in text, needle

    def test_render_when_empty(self):
        text = ProgressLedger(goal="g").render()
        assert "(nothing yet)" in text and "(none)" in text and "(not stated)" in text

    def test_to_dict_is_plain_data(self):
        led = ProgressLedger(goal="g")
        led.record("t", {}, "ok", False)
        d = led.to_dict()
        assert d == {"goal": "g", "done": ["t({}) -> ok"], "failed": [], "next": ""}

    def test_the_report_counts_every_call_even_past_the_cap(self):
        led = ProgressLedger(max_entries=3)
        for i in range(10):
            led.record("t", {"i": i}, "ok", False)
        for i in range(7):
            led.record("u", {"i": i}, "boom", True)
        text = led.render()
        assert len(led.done) == 3 and len(led.failed) == 3
        assert "Done (10 calls" in text and "Failed (7 calls" in text


class TestTracker:
    def test_three_errors_in_a_row_fire_even_with_different_calls(self):
        t = StuckTracker(3)
        assert t.observe("a", True) == (None, False)
        assert t.observe("b", True) == (None, False)
        reason, progressed = t.observe("c", True)
        assert reason == "3 tool errors in a row" and not progressed

    def test_three_identical_calls_fire(self):
        t = StuckTracker(3)
        assert t.observe("a", False) == (None, True)
        assert t.observe("b", False) == (None, True)
        assert t.observe("b", False) == (None, False)
        reason, progressed = t.observe("b", False)
        assert reason == "the same call repeated 3 times" and not progressed

    def test_distinct_successful_calls_never_fire_however_many(self):
        """Identical *results* are not a signal: a tool that always answers "ok" is healthy."""
        t = StuckTracker(3)
        for i in range(50):
            assert t.observe(f"write::{i}", False) == (None, True)

    def test_success_resets_the_error_streak(self):
        t = StuckTracker(3)
        t.observe("a", True)
        t.observe("b", True)
        assert t.observe("c", False) == (None, True)
        assert t.observe("d", True) == (None, False)      # streak restarted at 1

    def test_reset_clears_the_counters(self):
        t = StuckTracker(3)
        t.observe("a", True)
        t.observe("b", True)
        t.reset()
        assert t.observe("c", True) == (None, False)

    def test_signature_is_stable(self):
        assert _signature("t", {"b": 1, "a": 2}) == _signature("t", {"a": 2, "b": 1})


class TestReflectionLadder:
    def test_rungs_escalate(self):
        m1 = reflection_message(1, "3 tool errors in a row", "  - a")
        m2 = reflection_message(2, "3 tool errors in a row", "  - a failed thing")
        m3 = reflection_message(3, "x", "")
        m4 = reflection_message(4, "x", "")
        assert "recovery step 1" in m1 and "root cause" in m1
        assert "recovery step 2" in m2 and "a failed thing" in m2 and "NOT tried" in m2
        assert "read-only probe" in m3
        assert "still blocked" in m4

    def test_every_message_is_marked_as_framework_text(self):
        for rung in range(1, 5):
            assert reflection_message(rung, "r", "").startswith("\n\n[framework] You appear to be stuck (r)")

    def test_rungs_past_the_last_reuse_it(self):
        assert reflection_message(9, "r", "").endswith(REFLECTION_RUNGS[-1])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistence_tracker.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'ProgressLedger'`.

- [ ] **Step 3: Append the implementation to `agentx_dev/Runner/Persistence.py`**

```python
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
        self.reset()

    def reset(self) -> None:
        self.same = 0
        self.errors = 0

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_persistence_tracker.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Runner/Persistence.py tests/test_persistence_tracker.py
git commit -m "feat(persistence): progress ledger, stuck tracker, reflection ladder"
```

---

### Task 3: History compaction

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (append)
- Test: `tests/test_persistence_compaction.py`

**Interfaces:**
- Consumes: `_clip` (Task 2).
- Produces:
  - `NOTES_MARK = "[Progress notes -- earlier turns were compacted]"`, `NOTES_PROMPT`
  - `estimate_tokens(history) -> int`
  - `plan_compaction(history, task_index, keep_recent) -> Optional[Tuple[int, list]]` (returns `(tail_start, middle_messages)`)
  - `apply_compaction(history, task_index, tail, notes) -> None` (in place)
  - `_split_notes(content) -> (content_without_notes, prior_notes)`
  - `render_for_summary(middle, prior_notes="", cap=40_000) -> str`
  - `build_notes(summary, prior, failed_text) -> str`
  - `compact_history(history, *, task_index, keep_recent, summarize, failed_text="") -> bool`

Design reminders (from spec 4.5): the notes live **on the original task message** (a `str`, or the first text part of a list, so media stays untouched), so no two user turns become adjacent. The kept tail must start at an `assistant` message, which keeps role alternation valid and never separates a tool call from its result.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistence_compaction.py`:

```python
import json

from agentx_dev.Runner.Persistence import (
    NOTES_MARK, apply_compaction, build_notes, compact_history, estimate_tokens,
    plan_compaction, render_for_summary, _split_notes,
)


def tool_turn(i, size=10):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "t", "arguments": json.dumps({"i": i})}}]},
        {"role": "tool", "name": "t", "tool_call_id": f"c{i}", "content": "r" * size},
    ]


def native_history(turns, size=10):
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": "TASK"}]
    for i in range(turns):
        h.extend(tool_turn(i, size))
    return h


def pairs_intact(h):
    called = {tc["id"] for m in h if m.get("tool_calls") for tc in m["tool_calls"]}
    answered = {m["tool_call_id"] for m in h if m["role"] == "tool"}
    return called == answered


class TestEstimate:
    def test_text_counts_four_chars_per_token(self):
        assert estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100

    def test_media_counts_a_flat_amount(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100000}}
        assert estimate_tokens([{"role": "user", "content": [{"type": "text", "text": ""}, img]}]) == 1500

    def test_text_documents_count_by_length(self):
        doc = {"type": "document", "source": {"type": "text", "media_type": "text/csv", "data": "x" * 800}}
        assert estimate_tokens([{"role": "user", "content": [doc]}]) == 200

    def test_tool_calls_are_counted(self):
        assert estimate_tokens(native_history(3)) > estimate_tokens(native_history(0))


class TestPlan:
    def test_tail_starts_at_an_assistant_turn(self):
        h = native_history(6)
        tail, middle = plan_compaction(h, 1, keep_recent=4)
        assert h[tail]["role"] == "assistant"
        assert len(h) - tail >= 4 and middle

    def test_walks_back_when_the_cut_lands_on_a_tool_result(self):
        h = native_history(6)                 # [sys, task, a0, t0, a1, t1, ...]
        tail, _ = plan_compaction(h, 1, keep_recent=3)    # naive cut would be a tool message
        assert h[tail]["role"] == "assistant"

    def test_nothing_to_compact(self):
        assert plan_compaction([{"role": "user", "content": "T"}, {"role": "assistant", "content": "a"}], 0, 6) is None
        assert plan_compaction(native_history(1), 1, keep_recent=6) is None


class TestApply:
    def test_notes_go_on_the_task_message_and_pairs_survive(self):
        h = native_history(6)
        tail, _ = plan_compaction(h, 1, keep_recent=4)
        apply_compaction(h, 1, tail, "NOTES v1")
        assert h[1]["content"].endswith("NOTES v1") and NOTES_MARK in h[1]["content"]
        assert [m["role"] for m in h][:3] == ["system", "user", "assistant"]
        assert pairs_intact(h)

    def test_second_compaction_replaces_the_notes(self):
        h = native_history(6)
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v1")
        for i in range(6, 10):
            h.extend(tool_turn(i))
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v2")
        assert h[1]["content"].count(NOTES_MARK) == 1 and h[1]["content"].endswith("NOTES v2")
        assert pairs_intact(h)

    def test_media_in_the_task_is_untouched_and_roles_alternate(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        h = [{"role": "user", "content": [{"type": "text", "text": "TASK"}, img]}]
        for i in range(5):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        tail, _ = plan_compaction(h, 0, keep_recent=2)
        apply_compaction(h, 0, tail, "N")
        assert h[0]["content"][1] is img and h[0]["content"][0]["text"].endswith("N")
        roles = [m["role"] for m in h]
        assert all(a != b for a, b in zip(roles, roles[1:])), roles

    def test_a_media_only_task_message_keeps_a_single_notes_block(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        h = [{"role": "user", "content": [img]}]
        for i in range(5):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        apply_compaction(h, 0, plan_compaction(h, 0, 2)[0], "N1")
        for i in range(5, 9):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        apply_compaction(h, 0, plan_compaction(h, 0, 2)[0], "N2")
        text = "".join(p.get("text", "") for p in h[0]["content"] if isinstance(p, dict))
        assert text.count(NOTES_MARK) == 1 and "N2" in text and "N1" not in text

    def test_split_notes_round_trip(self):
        base, prior = _split_notes("TASK\n\n" + NOTES_MARK + "\nold notes")
        assert base == "TASK" and prior == "old notes"
        assert _split_notes("TASK") == ("TASK", "")


class TestCompactHistory:
    def test_uses_the_summary_and_appends_the_failed_list(self):
        h = native_history(6)
        seen = []
        ok = compact_history(h, task_index=1, keep_recent=4,
                             summarize=lambda prompt: seen.append(prompt) or "LEARNED: x=3",
                             failed_text="  - boom [rung 1]")
        assert ok
        assert "LEARNED: x=3" in h[1]["content"] and "boom [rung 1]" in h[1]["content"]
        assert "You are compacting" in seen[0]

    def test_summary_failure_falls_back_to_the_ledger(self):
        h = native_history(6)

        def boom(_):
            raise RuntimeError("summary model down")

        assert compact_history(h, task_index=1, keep_recent=4, summarize=boom,
                               failed_text="  - boom [rung 1]")
        assert "no summary was available" in h[1]["content"] and "boom [rung 1]" in h[1]["content"]

    def test_prior_notes_are_fed_to_the_next_summary(self):
        h = native_history(6)
        compact_history(h, task_index=1, keep_recent=4, summarize=lambda p: "FIRST NOTES")
        for i in range(6, 10):
            h.extend(tool_turn(i))
        seen = []
        compact_history(h, task_index=1, keep_recent=4, summarize=lambda p: seen.append(p) or "SECOND")
        assert "FIRST NOTES" in seen[0]

    def test_returns_false_when_there_is_nothing_to_do(self):
        assert compact_history(native_history(1), task_index=1, keep_recent=6, summarize=lambda p: "x") is False

    def test_render_for_summary_caps_huge_input(self):
        middle = [{"role": "user", "content": "z" * 5000} for _ in range(30)]
        text = render_for_summary(middle, cap=2000)
        assert "oldest turns dropped" in text and len(text) < 3500
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistence_compaction.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'NOTES_MARK'`.

- [ ] **Step 3: Append the implementation to `agentx_dev/Runner/Persistence.py`**

```python
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
    return out + [{"type": "text", "text": block}]    # keep the separator so a later compaction can split it off


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_persistence_compaction.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Runner/Persistence.py tests/test_persistence_compaction.py
git commit -m "feat(persistence): history compaction that keeps tool pairs and alternation valid"
```

---

### Task 4: `PersistentRun` (the per-run state), patient retries, helpers

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (append)
- Test: `tests/test_persistence_run.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3, plus `AgentCompletion.from_agent(..., outcome=, progress=)` from Task 1 and `CostBudgetExceeded` (already imported).
- Produces:
  - `is_transient(exc) -> bool`
  - `accepts_budget(fn) -> bool` (True when `fn` takes `_budget` or `**kwargs`)
  - `apply_persistence(runners, persistence)` context manager
  - `PersistenceMixin`: a base class for runners. Its `persistence` property overrides `max_iterations` with `persistence.max_turns` while set and restores the previous value when cleared (so a Supervisor can switch persistence on for an already-built runner). It also suspends the runner's tool-result cache (`self.registry.configure_cache(None, None)`) while set and restores it when cleared (spec 4.9). The runner must have set `self.max_iterations` and built `self.registry` before the first assignment to `self.persistence`.
  - `PersistentRun(cfg, goal, *, budget=None, verbose=False, clock=None, sleep=None, asleep=None)` with attributes `cfg`, `budget`, `ledger`, `tracker`, `rung`, `outcome`, `detail`, `working_history`, `tool_calls`, `steps`, `task_index`; methods `bind(working_history, tool_calls, steps, task_index)`, `emit(event)`, `drain() -> list`, `before_turn(model)`, `abefore_turn(model)` (async), `after_turn(history, calls)`, `patient(fn)`, `apatient(factory)` (async), `finish(outcome, detail="")`, `report() -> str`, `exit_completion(model_name, user_input) -> AgentCompletion`, `exit_events(model_name, user_input)` (generator).
  - `calls` passed to `after_turn` is a list of `(name, args, result_text, is_error)` tuples.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistence_run.py`:

```python
import asyncio
import json
from types import SimpleNamespace

import pytest

from agentx_dev import CostBudgetExceeded
from agentx_dev.Runner.Persistence import (
    BudgetExpired, NOTES_MARK, OUTCOME_OUT_OF_TIME, OUTCOME_STUCK, Persistence,
    PersistenceMixin, PersistentRun, RunStuck, accepts_budget, apply_persistence, is_transient,
)


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def make_run(cfg=None):
    clock = Clock()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    async def asleep(s):
        sleep(s)

    run = PersistentRun(cfg or Persistence(max_minutes=10), "the goal",
                        clock=clock, sleep=sleep, asleep=asleep)
    return run, clock, sleeps


def history(turns, size=10):
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": "TASK"}]
    for i in range(turns):
        h.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        h.append({"role": "tool", "name": "t", "tool_call_id": f"c{i}", "content": "r" * size})
    return h


def bind(run, h):
    run.bind(h, [], [], 1)
    return h


class TestTransient:
    @pytest.mark.parametrize("exc,expected", [
        (StatusError(429), True), (StatusError(503), True), (StatusError(408), True),
        (StatusError(401), False), (StatusError(400), False), (StatusError(404), False),
        (TimeoutError(), True), (ConnectionError(), True),
        (type("APITimeoutError", (Exception,), {})(), True),
        (type("RateLimitError", (Exception,), {})(), True),
        (ValueError("bug"), False), (KeyError("x"), False),
        (CostBudgetExceeded(1.0, 0.5), False),
    ])
    def test_classification(self, exc, expected):
        assert is_transient(exc) is expected


class TestPatientRetries:
    def test_retries_transient_errors_with_backoff(self):
        run, _, sleeps = make_run()
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise StatusError(429)
            return "ok"

        assert run.patient(fn) == "ok"
        assert sleeps == [1.0, 2.0]

    def test_does_not_retry_auth_or_programming_errors(self):
        run, _, sleeps = make_run()
        with pytest.raises(StatusError):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(401)))
        with pytest.raises(ValueError):
            run.patient(lambda: (_ for _ in ()).throw(ValueError("bug")))
        assert sleeps == []

    def test_gives_up_when_the_next_wait_would_pass_the_deadline(self):
        run, _, sleeps = make_run(Persistence(max_minutes=0.05))     # 3 seconds
        with pytest.raises(BudgetExpired):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(503)))
        assert sleeps == [1.0]

    def test_patient_retries_can_be_switched_off(self):
        run, _, sleeps = make_run(Persistence(max_minutes=10, patient_retries=False))
        with pytest.raises(StatusError):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(503)))
        assert sleeps == []

    def test_async_version(self):
        run, _, sleeps = make_run()
        attempts = []

        async def fn():
            attempts.append(1)
            if len(attempts) < 2:
                raise StatusError(503)
            return "ok"

        assert asyncio.run(run.apatient(fn)) == "ok"
        assert sleeps == [1.0]


class TestBudgetHook:
    def test_before_turn_raises_when_the_deadline_has_passed(self):
        run, clock, _ = make_run()
        bind(run, history(1))
        run.before_turn(SimpleNamespace())          # fine at t=0
        clock.t = 601
        with pytest.raises(BudgetExpired):
            run.before_turn(SimpleNamespace())

    def test_a_parent_budget_caps_the_run(self):
        from agentx_dev.Runner.Persistence import RunBudget
        clock = Clock()
        parent = RunBudget.start(1, clock)
        run = PersistentRun(Persistence(max_minutes=30), "g", budget=parent, clock=clock)
        assert run.budget.deadline == parent.deadline


class TestAfterTurn:
    def errors(self, run, h, n, start=0):
        for i in range(n):
            h.append({"role": "user", "content": "Error: boom"})
            run.after_turn(h, [("flaky", {"i": start + i}, "Error: boom", True)])

    def test_reflects_and_escalates_then_gets_stuck(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3, max_reflections=2))
        h = bind(run, history(0))
        self.errors(run, h, 3)
        assert "recovery step 1" in h[-1]["content"]
        assert run.drain() == [{"type": "reflect", "rung": 1, "reason": "3 tool errors in a row"}]
        self.errors(run, h, 3, start=3)
        assert "recovery step 2" in h[-1]["content"]
        with pytest.raises(RunStuck, match="3 tool errors in a row"):
            self.errors(run, h, 3, start=6)

    def test_progress_resets_the_ladder(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3, max_reflections=2))
        h = bind(run, history(0))
        self.errors(run, h, 3)
        assert run.rung == 1
        h.append({"role": "user", "content": "fine"})
        run.after_turn(h, [("steady", {"p": 1}, "all good", False)])
        assert run.rung == 0
        self.errors(run, h, 3, start=10)
        assert run.rung == 1 and "recovery step 1" in h[-1]["content"]

    def test_a_multi_call_turn_is_one_observation_batch(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3))
        h = bind(run, history(0))
        h.append({"role": "tool", "name": "a", "tool_call_id": "1", "content": "e"})
        h.append({"role": "tool", "name": "b", "tool_call_id": "2", "content": "e"})
        h.append({"role": "tool", "name": "c", "tool_call_id": "3", "content": "e"})
        run.after_turn(h, [("a", {}, "e", True), ("b", {}, "e", True), ("c", {}, "e", True)])
        assert run.rung == 1
        assert "recovery step 1" in h[-1]["content"] and "recovery step" not in h[-2]["content"]

    def test_the_ledger_sees_every_call(self):
        run, _, _ = make_run()
        h = bind(run, history(0))
        run.after_turn(h, [("t", {"x": 1}, "result line", False)])
        assert run.ledger.done == ['t({"x": 1}) -> result line']


class TestCompactionHook:
    def test_compacts_when_over_the_threshold(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))
        model = SimpleNamespace(Initialize=lambda messages: "SUMMARY-TEXT")
        run.ledger.record("t", {}, "boom", True)
        run.before_turn(model)
        ev = run.drain()
        assert ev and ev[0]["type"] == "compact" and ev[0]["after_tokens"] < ev[0]["before_tokens"]
        assert NOTES_MARK in h[1]["content"] and "SUMMARY-TEXT" in h[1]["content"]
        assert "boom" in h[1]["content"]
        assert h[2]["role"] == "assistant"

    def test_summary_failure_falls_back_to_the_ledger(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))

        def boom(messages):
            raise RuntimeError("down")

        run.before_turn(SimpleNamespace(Initialize=boom))
        assert "no summary was available" in h[1]["content"]

    def test_a_cost_error_in_the_summary_call_propagates(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        bind(run, history(8, size=200))

        def over(messages):
            raise CostBudgetExceeded(1.0, 0.5)

        with pytest.raises(CostBudgetExceeded):
            run.before_turn(SimpleNamespace(Initialize=over))

    def test_does_not_thrash(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))
        calls = []
        model = SimpleNamespace(Initialize=lambda messages: calls.append(1) or "S")
        run.before_turn(model)
        h.append({"role": "tool", "name": "t", "tool_call_id": "c7", "content": "ok"})   # tiny growth
        run.before_turn(model)
        assert len(calls) == 1

    def test_async_version(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))

        async def summarize(messages):
            return "ASYNC-SUMMARY"

        asyncio.run(run.abefore_turn(SimpleNamespace(async_initialize=summarize)))
        assert "ASYNC-SUMMARY" in h[1]["content"]


class TestReport:
    def test_out_of_time_report_and_completion(self):
        run, _, _ = make_run()
        h = bind(run, history(0))
        run.ledger.record("t", {}, "ok", False)
        run.finish(OUTCOME_OUT_OF_TIME)
        assert run.report().startswith("Stopped: the 10-minute time limit was reached.")
        c = run.exit_completion("GPT", "do it")
        assert c.outcome == "out_of_time" and c.content == run.report()
        assert c.progress["done"] == ["t({}) -> ok"] and c.query == "do it" and c.history == h

    def test_stuck_report_names_the_reason(self):
        run, _, _ = make_run()
        bind(run, history(0))
        run.finish(OUTCOME_STUCK, "3 tool errors in a row")
        assert "stuck after 4 recovery attempts (3 tool errors in a row)" in run.report()

    def test_exit_events_are_final_then_completion(self):
        run, _, _ = make_run()
        bind(run, history(0))
        run.finish(OUTCOME_STUCK, "x")
        events = list(run.exit_events("GPT", "q"))
        assert [e["type"] for e in events] == ["final", "completion"]
        assert events[0]["content"] == events[1]["completion"].content


class TestHelpers:
    def test_accepts_budget(self):
        assert accepts_budget(lambda q, _budget=None: 1)
        assert accepts_budget(lambda q, **kw: 1)
        assert not accepts_budget(lambda q: 1)

    def test_apply_persistence_sets_and_restores(self):
        p = Persistence()
        bare = SimpleNamespace(persistence=None)
        own = SimpleNamespace(persistence=Persistence(max_minutes=5))
        fake = SimpleNamespace()                       # no persistence attribute at all
        with apply_persistence([bare, own, fake], p):
            assert bare.persistence is p
            assert own.persistence.max_minutes == 5
            assert not hasattr(fake, "persistence")
        assert bare.persistence is None and own.persistence.max_minutes == 5

    def test_apply_persistence_with_none_is_a_noop(self):
        bare = SimpleNamespace(persistence=None)
        with apply_persistence([bare], None):
            assert bare.persistence is None


class TestMixin:
    class Runner(PersistenceMixin):
        def __init__(self):
            self.max_iterations = 4

    def test_persistence_overrides_max_iterations_and_restores_it(self):
        r = self.Runner()
        assert r.persistence is None
        r.persistence = Persistence(max_turns=50)
        assert r.max_iterations == 50
        r.persistence = None
        assert r.max_iterations == 4 and r.persistence is None

    def test_switching_between_two_settings_keeps_the_original_cap(self):
        r = self.Runner()
        r.persistence = Persistence(max_turns=50)
        r.persistence = Persistence(max_turns=70)
        assert r.max_iterations == 70
        r.persistence = None
        assert r.max_iterations == 4

    def test_rejects_the_wrong_type(self):
        with pytest.raises(TypeError, match="Persistence"):
            self.Runner().persistence = "yes"

    def test_persistence_suspends_the_tool_cache_and_restores_it(self):
        class FakeRegistry:
            def __init__(self):
                self.cache, self._cache_ttl = "CACHE", 300

            def configure_cache(self, cache, cache_ttl=None):
                self.cache, self._cache_ttl = cache, cache_ttl

        r = self.Runner()
        r.registry = FakeRegistry()
        r.persistence = Persistence()
        assert r.registry.cache is None
        r.persistence = Persistence(max_turns=5)          # switching settings keeps it suspended
        assert r.registry.cache is None
        r.persistence = None
        assert (r.registry.cache, r.registry._cache_ttl) == ("CACHE", 300)

    def test_apply_persistence_drives_a_real_mixin(self):
        r = self.Runner()
        with apply_persistence([r], Persistence(max_turns=9)):
            assert r.max_iterations == 9
        assert r.max_iterations == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistence_run.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'PersistentRun'`.

- [ ] **Step 3: Append the implementation to `agentx_dev/Runner/Persistence.py`**

```python
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
        self.working_history: List[Dict[str, Any]] = []
        self.tool_calls: List[Any] = []
        self.steps: List[str] = []
        self.task_index = 0
        self._events: List[Dict[str, Any]] = []
        self._floor = 0

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

    def finish(self, outcome: str, detail: str = "") -> None:
        self.outcome = outcome
        self.detail = detail

    def report(self) -> str:
        head = {
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_persistence_run.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Runner/Persistence.py tests/test_persistence_run.py
git commit -m "feat(persistence): PersistentRun state, patient retries, PersistenceMixin, budget helpers"
```

---

### Task 5: Honest outcomes in the sync runner (default mode)

This task changes **no** persistent behavior. It makes every sync run report how it ended, which is what lets the Supervisor stop treating "gave up" as success (Task 8).

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py` (five small edits inside `_iter_run`)
- Test: `tests/test_run_outcomes.py`

**Interfaces:**
- Consumes: `AgentCompletion.outcome` / `from_agent(outcome=...)` (Task 1).
- Produces: a local `outcome` variable in `_iter_run` (`"done"` | `"stuck"` | `"iteration_limit"`) that Task 6 extends; `completion.outcome` set on every sync run.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_outcomes.py`:

```python
"""Every run reports how it ended (default mode, sync runner)."""

from agentx_dev import AgentRunner, AgentType, StandardTool
from tests.conftest import MockModel


def calc():
    return StandardTool(func=lambda x: f"= {x}", name="calc", description="Compute.")


def run(model, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[calc()], verbose=False, **kw)


def test_a_final_answer_is_done(make_final_response):
    result = run(MockModel(script=[make_final_response("hi")])).invoke("q")
    assert result.outcome == "done" and result.progress is None


def test_running_out_of_iterations_is_iteration_limit(make_react):
    model = MockModel(script=[make_react("calc", str(i)) for i in range(6)])
    result = run(model, max_iterations=3).invoke("q")
    assert result.outcome == "iteration_limit" and "max_iterations" in result.content


def test_three_identical_calls_is_stuck(make_react):
    model = MockModel(script=[make_react("calc", "5")] * 10)
    result = run(model, max_iterations=10).invoke("q")
    assert result.outcome == "stuck" and "Terminated" in result.content


def test_repeated_malformed_json_is_iteration_limit():
    model = MockModel(script=lambda messages: "{ this is not json }")   # JSON-shaped but invalid, and nothing to salvage
    result = run(model, max_iterations=2).invoke("q")
    assert result.outcome == "iteration_limit" and "exhausted max_iterations" in result.content
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_run_outcomes.py -q -p no:cacheprovider`
Expected: 3 FAIL (`outcome` is always `"done"`), `test_a_final_answer_is_done` passes.

- [ ] **Step 3: Make the five edits in `agentx_dev/Runner/AgentRun.py`**

(a) Declare the variable. Replace
```python
        final_answer: Optional[str] = None
```
with
```python
        final_answer: Optional[str] = None
        outcome = "done"
```

(b) Native-mode force stop. Replace
```python
                if consecutive_identical_actions >= LOOP_FORCE_STOP:
                    names = ", ".join(sorted({c["name"] for c in non_respond})) or "<none>"
```
with
```python
                if consecutive_identical_actions >= LOOP_FORCE_STOP:
                    outcome = "stuck"
                    names = ", ".join(sorted({c["name"] for c in non_respond})) or "<none>"
```

(c) Text/function-calling force stop. Replace
```python
            if consecutive_identical_actions >= LOOP_FORCE_STOP:
                if tool_calls:
                    last = tool_calls[-1]
                    forced = (
```
with
```python
            if consecutive_identical_actions >= LOOP_FORCE_STOP:
                outcome = "stuck"
                if tool_calls:
                    last = tool_calls[-1]
                    forced = (
```

(d) Malformed-JSON exhaustion. Replace
```python
                    if count > self.max_iterations:
                        final_answer = (
                            "(framework: exhausted max_iterations after "
```
with
```python
                    if count > self.max_iterations:
                        outcome = "iteration_limit"
                        final_answer = (
                            "(framework: exhausted max_iterations after "
```

(e) Falling out of the loop, and the completion. Replace
```python
        if final_answer is None:
            summary_lines = [
```
with
```python
        if final_answer is None:
            outcome = "iteration_limit"
            summary_lines = [
```
and replace
```python
            content=final_answer,
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
        )
```
with
```python
            content=final_answer,
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
            outcome=outcome,
        )
```

- [ ] **Step 4: Run the tests and the full suite**

Run: `python -m pytest tests/test_run_outcomes.py -q -p no:cacheprovider`
Expected: 4 passed.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Runner/AgentRun.py tests/test_run_outcomes.py
git commit -m "feat(runner): every sync run reports how it ended (outcome)"
```

---

### Task 6: Persistent mode in the sync runner

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (append `run_persistent`, `_remember`)
- Modify: `agentx_dev/Runner/AgentRun.py` (imports, class bases, `__init__`, `_iter_run` split, hooks, `Initialize`)
- Test: `tests/test_persistent_runner.py`

**Interfaces:**
- Consumes: `PersistentRun`, `PersistenceMixin`, `RunBudget`, `BudgetExpired`, `RunStuck`, outcome constants (Tasks 1-4); the local `outcome` variable (Task 5).
- Produces:
  - `AgentRunner(..., persistence: Optional[Persistence] = None)`; attribute `runner.persistence` (property, assignable)
  - `AgentRunner.Initialize(..., _budget: Optional[RunBudget] = None)` (internal; the Supervisor passes the shared deadline)
  - `AgentRunner._iter_run(...)` (wrapper) and `AgentRunner._iter_run_core(user_input, chat_history, stream_tokens, media, state)`
  - `run_persistent(runner, user_input, chat_history, stream_tokens, media, budget)` generator
  - stream events `reflect` and `compact`; completions carry `outcome` and `progress`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistent_runner.py`:

```python
"""Persistent AgentRunner: recovery, budgets, compaction, events (sync)."""

import time

import pytest
from pydantic import BaseModel

from agentx_dev import AgentRunner, AgentType, CostBudgetExceeded, Persistence, StandardTool
from agentx_dev.Runner.Persistence import RunBudget
from tests.conftest import MockModel, make_final, make_react_response


def boom(x):
    raise RuntimeError(f"cannot do {x}")


FLAKY = StandardTool(func=boom, name="flaky", description="always fails")
STEADY = StandardTool(func=lambda x: f"ok {x}", name="steady", description="works")


def runner(model, tools=(FLAKY, STEADY), persistence=None, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False,
                       persistence=persistence or Persistence(max_minutes=5), **kw)


def text_of(history):
    return "\n".join(str(m.get("content")) for m in history)


def recovering(messages):
    """Fails the same way until a recovery message arrives, then changes approach."""
    last = str(messages[-1]["content"])
    if "ok x" in last:
        return make_final("fixed")
    if "recovery step" in last:
        return make_react_response("steady", "x")
    return make_react_response("flaky", "a")


class TestRecovery:
    def test_a_reflection_lets_the_model_change_approach(self):
        result = runner(MockModel(script=recovering)).invoke("make it work")
        assert result.outcome == "done" and result.content == "fixed"
        assert "recovery step 1" in text_of(result.history)
        assert len(result.progress["failed"]) == 3 and result.progress["goal"] == "make it work"

    def test_stuck_forever_ends_stuck_with_a_report(self):
        model = MockModel(script=lambda m: make_react_response("flaky", "a"))
        result = runner(model, persistence=Persistence(max_minutes=5, max_reflections=2)).invoke("go")
        assert result.outcome == "stuck"
        assert result.content.startswith("Stopped: stuck after 2 recovery attempts")
        assert text_of(result.history).count("recovery step") == 2
        assert result.progress["failed"]

    def test_progress_resets_the_ladder(self):
        plan = [("flaky", "1"), ("flaky", "2"), ("flaky", "3"), ("steady", "p"),
                ("flaky", "4"), ("flaky", "5"), ("flaky", "6")]
        turns = []

        def script(messages):
            turns.append(1)
            i = len(turns) - 1
            return make_react_response(*plan[i]) if i < len(plan) else make_final("done")

        result = runner(MockModel(script=script)).invoke("go")
        text = text_of(result.history)
        assert result.outcome == "done"
        assert text.count("recovery step 1") == 2 and "recovery step 2" not in text

    def test_stream_carries_the_reflect_event(self):
        events = list(runner(MockModel(script=recovering)).stream("make it work"))
        reflects = [e for e in events if e["type"] == "reflect"]
        assert reflects == [{"type": "reflect", "rung": 1, "reason": "3 tool errors in a row"}]
        assert events[-1]["type"] == "completion"


class TestBudgets:
    def test_time_limit_ends_the_run_cleanly(self):
        slow = StandardTool(func=lambda x: (time.sleep(0.1), f"ok {x}")[1], name="slow", description="slow")
        n = []

        def script(messages):
            n.append(1)
            return make_react_response("slow", str(len(n)))

        result = runner(MockModel(script=script), tools=[slow],
                        persistence=Persistence(max_minutes=0.001)).invoke("go")     # 60 ms
        assert result.outcome == "out_of_time" and "time limit" in result.content
        assert len(result.tool_calls) >= 1

    def test_cost_limit_ends_the_run_cleanly(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 3:
                raise CostBudgetExceeded(spent_usd=1.0, limit_usd=0.5)
            return make_react_response("steady", str(len(n)))

        result = runner(MockModel(script=script)).invoke("go")
        assert result.outcome == "out_of_budget" and "cost budget" in result.content
        assert len(result.tool_calls) == 2

    def test_a_spent_shared_budget_stops_before_any_model_call(self):
        spent = RunBudget(deadline=0.0, clock=lambda: 1.0)
        model = MockModel(script=[make_final("x")])
        result = runner(model).Initialize("go", _budget=spent)
        assert result.outcome == "out_of_time" and model.calls == []

    def test_a_transient_provider_error_is_retried(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)

        class Rate(Exception):
            status_code = 429

        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                raise Rate("slow down")
            return make_final("fine")

        result = runner(MockModel(script=script)).invoke("go")
        assert result.content == "fine" and len(n) == 2

    def test_a_non_transient_error_still_raises(self):
        def script(messages):
            raise ValueError("a real bug")

        with pytest.raises(ValueError, match="a real bug"):
            runner(MockModel(script=script)).invoke("go")


class TestCompaction:
    def test_long_runs_compact_and_keep_going(self):
        big = StandardTool(func=lambda x: f"call {x}: " + "data " * 100, name="big", description="big output")
        turns = []

        def script(messages):
            if len(messages) == 1 and "You are compacting" in str(messages[0]["content"]):
                return "NOTES: learned a lot"
            turns.append(1)
            return make_react_response("big", str(len(turns))) if len(turns) <= 10 else make_final("finished")

        cfg = Persistence(max_minutes=5, compact_at_tokens=400, keep_recent_turns=4)
        events = list(runner(MockModel(script=script), tools=[big], persistence=cfg).stream("go"))
        assert "compact" in [e["type"] for e in events]
        completion = [e for e in events if e["type"] == "completion"][0]["completion"]
        assert completion.outcome == "done" and completion.content == "finished"
        notes = [m for m in completion.history if m["role"] == "user" and "Progress notes" in str(m["content"])]
        assert notes and "NOTES: learned a lot" in str(notes[0]["content"])


class TestStructuredOutput:
    class Answer(BaseModel):
        answer: str

    def test_the_schema_still_applies_after_a_recovery(self):
        def script(messages):
            last = str(messages[-1]["content"])
            if "ok x" in last:
                return make_final('{"answer": "42"}')
            return recovering(messages)

        result = runner(MockModel(script=script), output_schema=self.Answer).invoke("go")
        assert result.outcome == "done" and result.output.answer == "42"

    def test_a_non_done_outcome_skips_coercion(self):
        model = MockModel(script=lambda m: make_react_response("flaky", "a"))
        result = runner(model, output_schema=self.Answer,
                        persistence=Persistence(max_minutes=5, max_reflections=1)).invoke("go")
        assert result.outcome == "stuck" and result.output is None


class TestDefaultModeUntouched:
    def test_no_persistence_keeps_the_iteration_cap(self):
        model = MockModel(script=[make_react_response("steady", str(i)) for i in range(10)])
        r = AgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False, max_iterations=3)
        assert r.persistence is None
        assert r.invoke("go").outcome == "iteration_limit"

    def test_persistence_replaces_the_cap_and_clearing_it_restores_the_cap(self):
        r = runner(MockModel(script=[]), persistence=Persistence(max_turns=77), max_iterations=3)
        assert r.max_iterations == 77
        r.persistence = None
        assert r.max_iterations == 3

    def test_the_tool_cache_is_suspended_while_persistent_and_restored_after(self):
        r = AgentRunner(model=MockModel(script=[]), agent=AgentType.ReAct, tools=[STEADY], verbose=False)
        original = r.registry.cache
        r.persistence = Persistence()
        assert r.registry.cache is None
        r.persistence = None
        assert r.registry.cache is original

    def test_a_persistent_run_always_executes_its_tools(self):
        calls = []
        counting = StandardTool(func=lambda x: (calls.append(x), f"ok {x}")[1], name="counting", description="d")
        model = MockModel(script=[make_react_response("counting", "same"),
                                  make_react_response("counting", "same"), make_final("done")])
        result = runner(model, tools=[counting]).invoke("go")
        assert result.outcome == "done" and calls == ["same", "same"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistent_runner.py -q -p no:cacheprovider`
Expected: FAIL (`TypeError: AgentRunner.__init__() got an unexpected keyword argument 'persistence'`).

- [ ] **Step 3: Append `run_persistent` to `agentx_dev/Runner/Persistence.py`**

```python
# ---------------------------------------------------------------------------
# Entry points the runners call
# ---------------------------------------------------------------------------

def _remember(runner: Any, user_input: str, content: str) -> None:
    """Mirror the runner's normal end-of-run memory write for early exits."""
    if getattr(runner, "auto_memory", False) and getattr(runner, "_memory", None):
        runner._memory.add_message("user", user_input)
        runner._memory.add_message("assistant", content)


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
    _remember(runner, user_input, events[-1]["completion"].content)
    yield from events
```

- [ ] **Step 4: Edit `agentx_dev/Runner/AgentRun.py`**

Use the Edit tool; every `old` below is unique in the file. If an edit reports a non-unique match, extend it with the neighbouring line shown.

(a) Imports. Replace
```python
from agentx_dev.ChatModel import BaseChatModel
```
with
```python
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.Persistence import Persistence, PersistenceMixin, RunBudget, run_persistent
```

(b) Class base. Replace `class AgentRunner:` with `class AgentRunner(PersistenceMixin):`.

(c) Constructor parameter. Replace
```python
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
    ):
        """Construct an ``AgentRunner``.
```
with
```python
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
        persistence: Optional[Persistence] = None,
    ):
        """Construct an ``AgentRunner``.
```
and document it: replace
```python
                genuinely is the answer.

        Raises:
            TypeError: If both ``Agent`` and ``agent`` are passed
```
with
```python
                genuinely is the answer.
            persistence: Opt-in ``Persistence(...)`` that keeps the run
                working through errors: it reflects and changes approach
                when stuck, compacts a long history, retries transient
                provider errors, and stops on ``max_minutes`` or the
                model's cost cap (``configure_limits(budget_usd=...)``) instead of ``max_iterations``
                (``max_turns`` is the backstop). The completion's
                ``outcome`` says how the run ended. Default ``None``
                keeps the ordinary loop.

        Raises:
            TypeError: If both ``Agent`` and ``agent`` are passed
```

(d) Store it. It must come after `max_iterations` is set and after the registry exists, because the property overrides the cap and suspends the registry's tool-result cache. Replace
```python
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
```
with
```python
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
        # After max_iterations and the registry exist: persistent runs override the
        # iteration cap and suspend the tool-result cache (see PersistenceMixin).
        self.persistence = persistence
```

(e) Split `_iter_run`. Replace
```python
    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
    ):
```
with
```python
    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ):
        """Run the agent loop as a generator of step events (event types are
        listed in ``_iter_run_core``). With ``persistence`` set the loop runs
        under a ``PersistentRun``: a time or cost limit, or an exhausted
        reflection ladder, ends the run with a normal completion whose
        ``outcome`` says why."""
        if self.persistence is None:
            yield from self._iter_run_core(user_input, chat_history, stream_tokens, media, None)
            return
        yield from run_persistent(self, user_input, chat_history, stream_tokens, media, _budget)

    def _iter_run_core(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        state: Any = None,
    ):
```

(f) Task message index. Replace
```python
        working_history.append({"role": "user",
                                "content": user_content(user_input, media)})
```
with
```python
        working_history.append({"role": "user",
                                "content": user_content(user_input, media)})
        task_index = len(working_history) - 1
```

(g) State wiring. Replace
```python
        final_answer: Optional[str] = None
        outcome = "done"
```
with
```python
        final_answer: Optional[str] = None
        outcome = "done"
        if state is not None:
            state.bind(working_history, tool_calls, steps, task_index)

        def _model_call(fn):
            # Persistent runs wait out transient provider errors (429/5xx/timeouts).
            return state.patient(fn) if state is not None else fn()
```
and replace
```python
        LOOP_FORCE_STOP = 3   # 3 identical calls in a row → abort
```
with
```python
        # Persistent runs use the stuck tracker (reflect, then end) instead of this abort.
        LOOP_FORCE_STOP = 3 if state is None else 10 ** 9
```

(h) Hook before each model call. Replace
```python
        while count <= self.max_iterations:
            if self.bind_tools_natively:
```
with
```python
        while count <= self.max_iterations:
            if state is not None:
                state.before_turn(self.model)
                yield from state.drain()
            if self.bind_tools_natively:
```

(i) Wrap the three model calls. Replace
```python
                call_result = self.model.call_with_tools(
                    messages=working_history,
                    tools=native_tool_specs,
                    force_tool=None,
                )
```
with
```python
                call_result = _model_call(lambda: self.model.call_with_tools(
                    messages=working_history,
                    tools=native_tool_specs,
                    force_tool=None,
                ))
```
replace
```python
                call_result = self.model.call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                )
```
with
```python
                call_result = _model_call(lambda: self.model.call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                ))
```
and replace
```python
                    response = self.model.Initialize(messages=working_history)
```
with
```python
                    response = _model_call(lambda: self.model.Initialize(messages=working_history))
```
(`stream_tokens=True` is not wrapped: a streamed response cannot be retried half-way.)

(j) Ledger note from the model's thought. Replace
```python
            thought = getattr(parser_instance, "Thought", None)
            if thought:
                yield {"type": "thought", "content": thought}
```
with
```python
            thought = getattr(parser_instance, "Thought", None)
            if thought:
                if state is not None:
                    state.note_thought(thought)
                yield {"type": "thought", "content": thought}
```

(k) Hook after tool results, native mode. Replace
```python
                for call, result in zip(non_respond, results):
                    is_error = isinstance(result, ToolError)
```
with
```python
                turn_obs: List[Any] = []
                for call, result in zip(non_respond, results):
                    is_error = isinstance(result, ToolError)
```
and replace
```python
                        "content": f"Error: {result}" if is_error else str(result),
                    })
                count += 1
                continue
```
with
```python
                        "content": f"Error: {result}" if is_error else str(result),
                    })
                    turn_obs.append((call["name"], call["input"], str(result), is_error))
                if state is not None:
                    state.after_turn(working_history, turn_obs)
                    yield from state.drain()
                count += 1
                continue
```

(l) Hook after tool results, text/function-calling mode. Replace
```python
            self._last_function_call_id = None

            count += 1

        # If we exited the loop without a Final_Answer
```
with
```python
            self._last_function_call_id = None

            if state is not None:
                state.after_turn(working_history,
                                 [(action, action_input, str(tool_response), is_error)])
                yield from state.drain()

            count += 1

        # If we exited the loop without a Final_Answer
```

(m) Completion carries the progress ledger. Replace
```python
            history=working_history,
            outcome=outcome,
        )
```
with
```python
            history=working_history,
            outcome=outcome,
            progress=state.ledger.to_dict() if state is not None else None,
        )
```

(n) `Initialize` takes the shared budget and skips coercion for a non-`done` persistent run. Replace
```python
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
    ) -> AgentCompletion:
        """The main agent execution loop.
```
with
```python
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ) -> AgentCompletion:
        """The main agent execution loop.
```
replace
```python
        for event in self._iter_run(user_input, ChatHistory, media=media):
```
with
```python
        for event in self._iter_run(user_input, ChatHistory, media=media, _budget=_budget):
```
and replace
```python
        if schema is not None:
            completion.output = self._coerce_to_schema(
```
with
```python
        # A persistent run that ended early has a report, not an answer: don't try to parse it.
        if schema is not None and not (self.persistence is not None and completion.outcome != "done"):
            completion.output = self._coerce_to_schema(
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `python -m pytest tests/test_persistent_runner.py -q -p no:cacheprovider`
Expected: all pass. If a timing test is flaky on a slow machine, raise the tool's `time.sleep` (not the budget) first.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Runner/Persistence.py agentx_dev/Runner/AgentRun.py tests/test_persistent_runner.py
git commit -m "feat(runner): persistent mode -- reflect, compact, patient retries, time/cost limits"
```

---

### Task 7: Honest outcomes and persistent mode in the async runner

**Files:**
- Modify: `agentx_dev/Runner/Persistence.py` (append `run_persistent_async`)
- Modify: `agentx_dev/Runner/AsyncAgentRun.py`
- Test: `tests/test_persistent_runner_async.py`

**Interfaces:**
- Consumes: everything from Task 6; `PersistentRun.abefore_turn`, `apatient`, `after_turn`, `drain` (Task 4).
- Produces: `AsyncAgentRunner(..., persistence=...)`; `AsyncAgentRunner.Initialize(..., _budget=None)` (wrapper) over `_initialize_core(..., state)`; `run_persistent_async(runner, user_input, budget, run)`; `completion.outcome` on every async run (`"done"` or `"iteration_limit"` in default mode; the async loop has never had the 3-identical-calls abort, so it never reports `"stuck"` without persistence).
- The async runner emits no stream events (its `astream` replays a finished run); the `reflect`/`compact` events are drained and dropped. `completion.progress` and the log lines still show them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persistent_runner_async.py`:

```python
"""Persistent AsyncAgentRunner: the sync scenarios, on the async loop."""

import asyncio
import time

import pytest
from pydantic import BaseModel

from agentx_dev import AgentType, AsyncAgentRunner, CostBudgetExceeded, Persistence, StandardTool
from agentx_dev.Runner.Persistence import RunBudget
from tests.conftest import MockModel, make_final, make_react_response


def boom(x):
    raise RuntimeError(f"cannot do {x}")


FLAKY = StandardTool(func=boom, name="flaky", description="always fails")
STEADY = StandardTool(func=lambda x: f"ok {x}", name="steady", description="works")


def runner(model, tools=(FLAKY, STEADY), persistence=None, **kw):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False,
                            persistence=persistence or Persistence(max_minutes=5), **kw)


def text_of(history):
    return "\n".join(str(m.get("content")) for m in history)


def recovering(messages):
    last = str(messages[-1]["content"])
    if "ok x" in last:
        return make_final("fixed")
    if "recovery step" in last:
        return make_react_response("steady", "x")
    return make_react_response("flaky", "a")


class TestDefaultMode:
    def test_a_final_answer_is_done(self):
        r = AsyncAgentRunner(model=MockModel(script=[make_final("hi")]), agent=AgentType.ReAct,
                             tools=[STEADY], verbose=False)
        assert asyncio.run(r.ainvoke("q")).outcome == "done"

    def test_running_out_of_iterations_is_iteration_limit(self):
        model = MockModel(script=[make_react_response("steady", str(i)) for i in range(6)])
        r = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False,
                             max_iterations=3)
        assert asyncio.run(r.ainvoke("q")).outcome == "iteration_limit"


class TestRecovery:
    def test_a_reflection_lets_the_model_change_approach(self):
        result = asyncio.run(runner(MockModel(script=recovering)).ainvoke("make it work"))
        assert result.outcome == "done" and result.content == "fixed"
        assert "recovery step 1" in text_of(result.history)
        assert len(result.progress["failed"]) == 3

    def test_stuck_forever_ends_stuck_with_a_report(self):
        model = MockModel(script=lambda m: make_react_response("flaky", "a"))
        r = runner(model, persistence=Persistence(max_minutes=5, max_reflections=2))
        result = asyncio.run(r.ainvoke("go"))
        assert result.outcome == "stuck" and result.content.startswith("Stopped: stuck after 2")
        assert text_of(result.history).count("recovery step") == 2

    def test_progress_resets_the_ladder(self):
        plan = [("flaky", "1"), ("flaky", "2"), ("flaky", "3"), ("steady", "p"),
                ("flaky", "4"), ("flaky", "5"), ("flaky", "6")]
        turns = []

        def script(messages):
            turns.append(1)
            i = len(turns) - 1
            return make_react_response(*plan[i]) if i < len(plan) else make_final("done")

        result = asyncio.run(runner(MockModel(script=script)).ainvoke("go"))
        text = text_of(result.history)
        assert result.outcome == "done"
        assert text.count("recovery step 1") == 2 and "recovery step 2" not in text


class TestBudgets:
    def test_time_limit(self):
        slow = StandardTool(func=lambda x: (time.sleep(0.1), f"ok {x}")[1], name="slow", description="slow")
        n = []

        def script(messages):
            n.append(1)
            return make_react_response("slow", str(len(n)))

        r = runner(MockModel(script=script), tools=[slow], persistence=Persistence(max_minutes=0.001))
        result = asyncio.run(r.ainvoke("go"))
        assert result.outcome == "out_of_time" and "time limit" in result.content

    def test_cost_limit(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 3:
                raise CostBudgetExceeded(spent_usd=1.0, limit_usd=0.5)
            return make_react_response("steady", str(len(n)))

        result = asyncio.run(runner(MockModel(script=script)).ainvoke("go"))
        assert result.outcome == "out_of_budget" and len(result.tool_calls) == 2

    def test_a_spent_shared_budget_stops_before_any_model_call(self):
        spent = RunBudget(deadline=0.0, clock=lambda: 1.0)
        model = MockModel(script=[make_final("x")])
        result = asyncio.run(runner(model).Initialize("go", _budget=spent))
        assert result.outcome == "out_of_time" and model.calls == []

    def test_a_transient_error_is_retried(self, monkeypatch):
        async def fast(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", fast)

        class Rate(Exception):
            status_code = 429

        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                raise Rate("slow down")
            return make_final("fine")

        result = asyncio.run(runner(MockModel(script=script)).ainvoke("go"))
        assert result.content == "fine" and len(n) == 2

    def test_a_non_transient_error_still_raises(self):
        def script(messages):
            raise ValueError("a real bug")

        with pytest.raises(ValueError, match="a real bug"):
            asyncio.run(runner(MockModel(script=script)).ainvoke("go"))


class TestCompaction:
    def test_long_runs_compact_and_keep_going(self):
        big = StandardTool(func=lambda x: f"call {x}: " + "data " * 100, name="big", description="big output")
        turns = []

        def script(messages):
            if len(messages) == 1 and "You are compacting" in str(messages[0]["content"]):
                return "NOTES: learned a lot"
            turns.append(1)
            return make_react_response("big", str(len(turns))) if len(turns) <= 10 else make_final("finished")

        cfg = Persistence(max_minutes=5, compact_at_tokens=400, keep_recent_turns=4)
        result = asyncio.run(runner(MockModel(script=script), tools=[big], persistence=cfg).ainvoke("go"))
        assert result.outcome == "done" and result.content == "finished"
        notes = [m for m in result.history if m["role"] == "user" and "Progress notes" in str(m["content"])]
        assert notes and "NOTES: learned a lot" in str(notes[0]["content"])


class TestStructuredOutput:
    class Answer(BaseModel):
        answer: str

    def test_the_schema_still_applies_after_a_recovery(self):
        def script(messages):
            if "ok x" in str(messages[-1]["content"]):
                return make_final('{"answer": "42"}')
            return recovering(messages)

        result = asyncio.run(runner(MockModel(script=script), output_schema=self.Answer).ainvoke("go"))
        assert result.outcome == "done" and result.output.answer == "42"

    def test_a_non_done_outcome_skips_coercion(self):
        model = MockModel(script=lambda m: make_react_response("flaky", "a"))
        r = runner(model, output_schema=self.Answer, persistence=Persistence(max_minutes=5, max_reflections=1))
        result = asyncio.run(r.ainvoke("go"))
        assert result.outcome == "stuck" and result.output is None


def test_persistence_replaces_the_cap_and_clearing_it_restores_the_cap():
    r = runner(MockModel(script=[]), persistence=Persistence(max_turns=77), max_iterations=3)
    assert r.max_iterations == 77
    r.persistence = None
    assert r.max_iterations == 3


def test_the_tool_cache_is_suspended_while_persistent_and_restored_after():
    r = AsyncAgentRunner(model=MockModel(script=[]), agent=AgentType.ReAct, tools=[STEADY], verbose=False)
    original = r.registry.cache
    r.persistence = Persistence()
    assert r.registry.cache is None
    r.persistence = None
    assert r.registry.cache is original


def test_a_persistent_run_always_executes_its_tools():
    calls = []
    counting = StandardTool(func=lambda x: (calls.append(x), f"ok {x}")[1], name="counting", description="d")
    model = MockModel(script=[make_react_response("counting", "same"),
                              make_react_response("counting", "same"), make_final("done")])
    result = asyncio.run(runner(model, tools=[counting]).ainvoke("go"))
    assert result.outcome == "done" and calls == ["same", "same"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_persistent_runner_async.py -q -p no:cacheprovider`
Expected: FAIL (`unexpected keyword argument 'persistence'`).

- [ ] **Step 3: Append `run_persistent_async` to `agentx_dev/Runner/Persistence.py`**

```python
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
    _remember(runner, user_input, completion.content)
    return completion
```

- [ ] **Step 4: Edit `agentx_dev/Runner/AsyncAgentRun.py`**

Use the Edit tool; extend an `old` with a neighbouring line if it reports a non-unique match.

(a) Imports. Replace
```python
from agentx_dev.ChatModel import BaseChatModel
```
with
```python
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.Persistence import Persistence, PersistenceMixin, RunBudget, run_persistent_async
```

(b) Class base. Replace `class AsyncAgentRunner:` with `class AsyncAgentRunner(PersistenceMixin):`.

(c) Constructor. Replace
```python
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
    ):
        """Construct an ``AsyncAgentRunner``.
```
with
```python
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
        persistence: Optional[Persistence] = None,
    ):
        """Construct an ``AsyncAgentRunner``.
```
and, after `max_iterations` is set and the registry exists, replace
```python
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
```
with
```python
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
        # After max_iterations and the registry exist: persistent runs override the
        # iteration cap and suspend the tool-result cache (see PersistenceMixin).
        self.persistence = persistence
```

(d) Split `Initialize`. Replace the whole signature
```python
    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
    ) -> AgentCompletion:
```
with
```python
    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ) -> AgentCompletion:
        if self.persistence is None:
            return await self._initialize_core(
                user_input, ChatHistory, stream, chat_history=chat_history,
                output_schema=output_schema, media=media, state=None)
        return await run_persistent_async(
            self, user_input, _budget,
            lambda state: self._initialize_core(
                user_input, ChatHistory, stream, chat_history=chat_history,
                output_schema=output_schema, media=media, state=state))

    async def _initialize_core(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
        state: Any = None,
    ) -> AgentCompletion:
```

(e) Task index. Replace
```python
        working_history.append({"role": "user",
                                "content": user_content(user_input, media)})
```
with
```python
        working_history.append({"role": "user",
                                "content": user_content(user_input, media)})
        task_index = len(working_history) - 1
```

(f) State wiring. Replace
```python
        final_answer: Optional[str] = None
```
with
```python
        final_answer: Optional[str] = None
        if state is not None:
            state.bind(working_history, tool_calls, steps, task_index)

        async def _model_call(factory):
            # Persistent runs wait out transient provider errors (429/5xx/timeouts).
            if state is None:
                return await factory()
            return await state.apatient(factory)
```

(g) Hook before each model call. Replace
```python
        while count <= self.max_iterations:
            if self.bind_tools_natively:
```
with
```python
        while count <= self.max_iterations:
            if state is not None:
                await state.abefore_turn(self.model)
                state.drain()          # the async runner has no event stream; drop them
            if self.bind_tools_natively:
```

(h) Wrap the three model calls. Replace
```python
                call_result = await self.model.async_call_with_tools(
                    messages=working_history,
                    tools=native_tool_specs,
                    force_tool=None,
                )
```
with
```python
                call_result = await _model_call(lambda: self.model.async_call_with_tools(
                    messages=working_history,
                    tools=native_tool_specs,
                    force_tool=None,
                ))
```
replace
```python
                call_result = await self.model.async_call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                )
```
with
```python
                call_result = await _model_call(lambda: self.model.async_call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                ))
```
and replace
```python
                response = await self.model.async_initialize(messages=working_history)
```
with
```python
                response = await _model_call(lambda: self.model.async_initialize(messages=working_history))
```

(i) Ledger note from the model's thought. Replace
```python
            thought = getattr(parser_instance, "Thought", None)
            if thought and self.verbose:
```
with
```python
            thought = getattr(parser_instance, "Thought", None)
            if thought and state is not None:
                state.note_thought(thought)
            if thought and self.verbose:
```

(j) Hook after tool results, native mode. Replace
```python
                for call, result in zip(non_respond, results):
                    if isinstance(result, BaseException):
```
with
```python
                turn_obs: List[Any] = []
                for call, result in zip(non_respond, results):
                    if isinstance(result, BaseException):
```
and replace
```python
                        "content": f"Error: {result}" if is_error else str(result),
                    })

                count += 1
                continue  # Skip the legacy single-action handling below.
```
with
```python
                        "content": f"Error: {result}" if is_error else str(result),
                    })
                    turn_obs.append((call["name"], call["input"], str(result), is_error))

                if state is not None:
                    state.after_turn(working_history, turn_obs)
                    state.drain()

                count += 1
                continue  # Skip the legacy single-action handling below.
```

(k) Hook after tool results, text/function-calling mode. Replace
```python
                    result=str(tool_response),
                ))

            count += 1
```
with
```python
                    result=str(tool_response),
                ))

            if state is not None:
                state.after_turn(working_history, [
                    (action, action_input, str(tool_response), isinstance(tool_response, ToolError))])
                state.drain()

            count += 1
```

(l) Outcome, completion and coercion. Replace
```python
        completion = AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=user_input,
            content=final_answer or "No final answer returned.",
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
        )
```
with
```python
        outcome = "done" if final_answer is not None else "iteration_limit"
        completion = AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=user_input,
            content=final_answer or "No final answer returned.",
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
            outcome=outcome,
            progress=state.ledger.to_dict() if state is not None else None,
        )
```
and replace
```python
        if _schema is not None:
            # Reuse the sync coercer
```
with
```python
        # A persistent run that ended early has a report, not an answer: don't try to parse it.
        if _schema is not None and not (state is not None and outcome != "done"):
            # Reuse the sync coercer
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `python -m pytest tests/test_persistent_runner_async.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Runner/Persistence.py agentx_dev/Runner/AsyncAgentRun.py tests/test_persistent_runner_async.py
git commit -m "feat(runner): persistent mode and honest outcomes in the async runner"
```

---

### Task 8: Honest outcomes in the Supervisor (default mode, sync and async)

A specialist that gave up must stop looking like a success. This is the one change in the plan that affects agents which do not opt in (spec 4.1).

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Test: `tests/test_supervisor_outcomes.py`

**Interfaces:**
- Consumes: `AgentCompletion.outcome` / `.progress` (Task 1); outcome constants and `Persistence`, `RunBudget`, `accepts_budget`, `apply_persistence`, `_clip` from `agentx_dev.Runner.Persistence`; `CostBudgetExceeded` from `agentx_dev.ChatModel`.
- Produces (Tasks 10-11 rely on these exact names):
  - `SubtaskResult.outcome: str = "done"`, `.progress: Optional[Dict[str, Any]] = None`, `.superseded: bool = False`
  - `SupervisorResult.outcome: str = "done"`
  - module functions `_outcome_of(completion) -> str`, `_progress_of(completion) -> Optional[dict]`, `_unfinished_reason(outcome) -> str`, `_result_failed(r) -> bool`, `_result_done(r) -> bool`, `_supervisor_outcome(results, budget_reason=None) -> str`
  - behavior: a returned `outcome != "done"` is a rejected attempt in `Supervisor._dispatch_with_retry` and `AsyncSupervisor._run_subtask` (retried up to `max_subtask_retries`, then returned with `error` set); `SupervisorResult.outcome` is always computed (`done` / `partial` / `stuck`), and is `stuck` when no plan could be made.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_outcomes.py`:

```python
"""A specialist that gave up is a failed attempt, not a success (default mode)."""

import asyncio
import json
from types import SimpleNamespace

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Supervisor import AsyncSupervisor, Supervisor
from tests.conftest import MockModel, make_react_response


def plan_json(*steps):
    return json.dumps({"plan": list(steps)})


def one_step(agent):
    return plan_json({"agent": agent, "query": "do the thing"})


class Scripted:
    """Fake specialist: each call returns the next (content, outcome)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.tools = []

    def Initialize(self, query):
        self.calls.append(query)
        content, outcome = self.results.pop(0) if self.results else ("ok", "done")
        return SimpleNamespace(content=content, outcome=outcome, output=None)


class AsyncScripted(Scripted):
    async def Initialize(self, query):          # type: ignore[override]
        return Scripted.Initialize(self, query)


def sup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 1)
    return Supervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False, **kw)


def asup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 1)
    return AsyncSupervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False, **kw)


class TestSync:
    def test_a_specialist_that_gave_up_is_retried_and_can_recover(self):
        worker = Scripted(("gave up", "stuck"), ("fixed", "done"))
        result = sup(MockModel(script=[one_step("worker"), "Final."]), {"worker": worker}).run("task")
        assert result.subtasks[0].error is None and result.subtasks[0].content == "fixed"
        assert len(worker.calls) == 2 and "outcome 'stuck'" in worker.calls[1]
        assert result.outcome == "done"

    def test_a_specialist_that_never_finishes_is_flagged_not_accepted(self):
        worker = Scripted(("a", "iteration_limit"), ("b", "iteration_limit"))
        result = sup(MockModel(script=[one_step("worker"), "Final."]), {"worker": worker}).run("task")
        sub = result.subtasks[0]
        assert sub.outcome == "iteration_limit" and "iteration_limit" in sub.error
        assert sub.content == "b"                       # content is preserved
        assert result.outcome == "stuck"

    def test_no_retries_configured(self):
        worker = Scripted(("a", "stuck"))
        result = sup(MockModel(script=[one_step("worker"), "Final."]), {"worker": worker},
                     max_subtask_retries=0).run("task")
        assert len(worker.calls) == 1 and result.subtasks[0].error

    def test_partial_when_one_step_finishes_and_another_does_not(self):
        plan = plan_json({"agent": "a", "query": "q1"}, {"agent": "b", "query": "q2"})
        result = sup(MockModel(script=[plan, "Final."]),
                     {"a": Scripted(("fine", "done")), "b": Scripted(("no", "stuck"))},
                     max_subtask_retries=0).run("task")
        assert result.outcome == "partial"

    def test_everything_done_is_done(self):
        result = sup(MockModel(script=[one_step("worker"), "Final."]),
                     {"worker": Scripted(("fine", "done"))}).run("task")
        assert result.outcome == "done"

    def test_no_plan_is_stuck(self):
        result = sup(MockModel(script=["this is not json"]), {"worker": Scripted()}).run("task")
        assert result.content == "Supervisor failed to produce a valid plan." and result.outcome == "stuck"

    def test_a_completion_without_an_outcome_counts_as_done(self):
        legacy = SimpleNamespace(tools=[], Initialize=lambda query: SimpleNamespace(content="old style"))
        result = sup(MockModel(script=[one_step("worker"), "Final."]), {"worker": legacy}).run("task")
        assert result.subtasks[0].outcome == "done" and result.outcome == "done"

    def test_a_real_runner_that_runs_out_of_iterations_is_flagged(self):
        steady = StandardTool(func=lambda x: f"ok {x}", name="steady", description="works")
        spec_model = MockModel(script=lambda m: make_react_response("steady", str(len(m))))
        specialist = AgentRunner(model=spec_model, agent=AgentType.ReAct, tools=[steady],
                                 verbose=False, max_iterations=2)
        result = sup(MockModel(script=[one_step("worker"), "Final."]), {"worker": specialist}).run("task")
        sub = result.subtasks[0]
        assert sub.outcome == "iteration_limit" and "iteration_limit" in sub.error
        assert result.outcome == "stuck"


class TestAsync:
    def test_a_specialist_that_gave_up_is_retried_and_can_recover(self):
        worker = AsyncScripted(("gave up", "stuck"), ("fixed", "done"))
        result = asyncio.run(asup(MockModel(script=[one_step("worker"), "Final."]), {"worker": worker}).run("task"))
        assert result.subtasks[0].error is None and result.subtasks[0].content == "fixed"
        assert len(worker.calls) == 2 and result.outcome == "done"

    def test_a_specialist_that_never_finishes_is_flagged_not_accepted(self):
        worker = AsyncScripted(("a", "iteration_limit"), ("b", "iteration_limit"))
        result = asyncio.run(asup(MockModel(script=[one_step("worker"), "Final."]), {"worker": worker}).run("task"))
        assert "iteration_limit" in result.subtasks[0].error and result.outcome == "stuck"

    def test_partial(self):
        plan = plan_json({"agent": "a", "query": "q1"}, {"agent": "b", "query": "q2"})
        result = asyncio.run(asup(MockModel(script=[plan, "Final."]),
                                  {"a": AsyncScripted(("fine", "done")), "b": AsyncScripted(("no", "stuck"))},
                                  max_subtask_retries=0).run("task"))
        assert result.outcome == "partial"

    def test_no_plan_is_stuck(self):
        result = asyncio.run(asup(MockModel(script=["not json"]), {"worker": AsyncScripted()}).run("task"))
        assert result.outcome == "stuck"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_supervisor_outcomes.py -q -p no:cacheprovider`
Expected: FAIL (`SubtaskResult` has no `outcome`; gave-up results are accepted).

- [ ] **Step 3: Edit `agentx_dev/Supervisor.py`**

(a) Imports. Replace
```python
from agentx_dev.ChatModel import BaseChatModel
```
with
```python
from agentx_dev.ChatModel import BaseChatModel, CostBudgetExceeded
```
and replace
```python
from agentx_dev.Tools import logger
```
with
```python
from agentx_dev.Tools import logger
from agentx_dev.Runner.Persistence import (
    OUTCOME_DONE, OUTCOME_OUT_OF_BUDGET, OUTCOME_OUT_OF_TIME, OUTCOME_PARTIAL, OUTCOME_STUCK,
    Persistence, RunBudget, _clip, accepts_budget, apply_persistence,
)
```

(b) Result fields. Replace
```python
    skipped: bool = False

    model_config = {"arbitrary_types_allowed": True}
```
with
```python
    skipped: bool = False
    # How the specialist's run ended: "done", "stuck", "iteration_limit",
    # "out_of_time", "out_of_budget". Anything but "done" is a failed attempt.
    outcome: str = "done"
    # Persistent specialists attach their progress ledger (goal/done/failed/next).
    progress: Optional[Dict[str, Any]] = None
    # Set when a recovery round replaced this failed step; superseded failures no longer count.
    superseded: bool = False

    model_config = {"arbitrary_types_allowed": True}
```
and replace
```python
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan
```
with
```python
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan
    # "done" | "partial" | "stuck" | "out_of_time" | "out_of_budget"
    outcome: str = "done"
```

(c) Helper functions. Insert immediately after `_format_results_block` (before `def _build_augmented_query(`):
```python
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
```

(d) Sync `_dispatch_with_retry`. Replace
```python
                    # Preserve the runner's validated Pydantic instance so
                    # downstream steps get typed data, not just prose.
                    output=getattr(completion, "output", None),
                )
```
with
```python
                    # Preserve the runner's validated Pydantic instance so
                    # downstream steps get typed data, not just prose.
                    output=getattr(completion, "output", None),
                    outcome=_outcome_of(completion),
                    progress=_progress_of(completion),
                )
```
and replace
```python
            ok, reason = self._evaluate_success(result)
            if ok:
                return result
```
with
```python
            if result.outcome != OUTCOME_DONE:
                ok, reason = False, _unfinished_reason(result.outcome)
            else:
                ok, reason = self._evaluate_success(result)
            if ok:
                return result
```

(e) Async `_run_subtask`. Replace
```python
                candidate = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    output=getattr(completion, "output", None),
                )
```
with
```python
                candidate = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    output=getattr(completion, "output", None),
                    outcome=_outcome_of(completion),
                    progress=_progress_of(completion),
                )
```
and replace
```python
            ok, reason = self._evaluate_success(candidate)
            if ok:
                result = candidate
                break
```
with
```python
            if candidate.outcome != OUTCOME_DONE:
                ok, reason = False, _unfinished_reason(candidate.outcome)
            else:
                ok, reason = self._evaluate_success(candidate)
            if ok:
                result = candidate
                break
```

(f) `SupervisorResult.outcome` in both supervisors. Replace (sync no-plan)
```python
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
            )
```
with
```python
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
                outcome=OUTCOME_STUCK,
            )
```
replace (async no-plan)
```python
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[], subtasks=[],
            )
```
with
```python
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[], subtasks=[],
                outcome=OUTCOME_STUCK,
            )
```
replace (sync final)
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
        )
```
with
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
            outcome=_supervisor_outcome(subtask_results),
        )
```
and replace (async final)
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
        )
```
with
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
            outcome=_supervisor_outcome(subtask_results),
        )
```

- [ ] **Step 4: Run the tests and the full suite**

Run: `python -m pytest tests/test_supervisor_outcomes.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures. If an existing Supervisor test now sees a retry it did not expect because its fake specialist returns a non-`done` outcome, that test is describing the old behavior: fix the test's expectation, not the code, and note it in the commit message.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_outcomes.py
git commit -m "feat(supervisor): a specialist that gave up is a failed attempt, not a success; results report an outcome"
```

---

### Task 9: Extract `_run_plan` in both supervisors (pure refactor)

The recovery rounds in Tasks 10-11 run the same execution code more than once, so it becomes a method. **No behavior change**: the existing suite is the test, plus one structural test.

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Test: `tests/test_supervisor_run_plan.py`

**Interfaces:**
- Consumes: the current bodies of `Supervisor.stream` and `AsyncSupervisor.astream`.
- Produces:
  - `Supervisor._run_plan(self, plan, subtask_results, results_by_id, spawn_rewrites)`: a generator yielding the same `dispatch` / `spawn` / `subtask_result` events the execution block yields today; mutates the three passed-in containers.
  - `AsyncSupervisor._run_plan(self, plan, subtask_results, results_by_id)`: an async generator yielding the same events; mutates the two passed-in containers.

- [ ] **Step 1: Write the structural test**

Create `tests/test_supervisor_run_plan.py`:

```python
"""_run_plan: the execution block, callable on its own."""

import asyncio
from types import SimpleNamespace

from agentx_dev.Supervisor import AsyncSupervisor, Supervisor
from tests.conftest import MockModel


class Runner:
    tools = []

    def Initialize(self, query):
        return SimpleNamespace(content="scraped")


class AsyncRunner:
    tools = []

    async def Initialize(self, query):
        return SimpleNamespace(content="scraped")


PLAN = [{"id": "a", "agent": "worker", "query": "q"}]


def test_sync_run_plan_streams_events_and_fills_the_shared_containers():
    sup = Supervisor(model=MockModel(script=[]), agents={"worker": ("w", Runner())}, verbose=False)
    results, by_id = [], {}
    events = list(sup._run_plan(PLAN, results, by_id, {}))
    assert [e["type"] for e in events] == ["dispatch", "subtask_result"]
    assert [r.step_id for r in results] == ["a"] and by_id["a"].content == "scraped"


def test_async_run_plan_streams_events_and_fills_the_shared_containers():
    sup = AsyncSupervisor(model=MockModel(script=[]), agents={"worker": ("w", AsyncRunner())}, verbose=False)
    results, by_id = [], {}

    async def collect():
        return [e async for e in sup._run_plan(PLAN, results, by_id)]

    events = asyncio.run(collect())
    assert [e["type"] for e in events] == ["dispatch", "subtask_result"]
    assert [r.step_id for r in results] == ["a"] and by_id["a"].content == "scraped"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_supervisor_run_plan.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: ... has no attribute '_run_plan'`.

- [ ] **Step 3: Extract the sync block**

In `Supervisor.stream`:

1. **Cut** from (and including) the comment line `        # 3.3: DAG mode fires when ANY step declares depends_on. Dep-free` down to (but excluding) `        yield {"type": "synthesize_start"}`. The block starts with `dag_mode = _plan_uses_deps(plan)` and ends with the loop's final `if self.verbose:\n                _log_result(sub_result)`.
2. **Paste** it as the body of a new method placed immediately before `def stream`, with this header (the block is already indented 8 spaces, which is correct for a method body):
```python
    def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        spawn_rewrites: Dict[str, str],
    ):
        """Execute ``plan``: dispatch each step (spawning specialists, cascading
        failures, evaluating ``skip_when``) and yield ``dispatch`` / ``spawn`` /
        ``subtask_result`` events. Results are appended to the shared
        ``subtask_results`` / ``results_by_id`` so several rounds can build on
        one another."""
```
3. In the pasted block, **delete** the three initialising lines (they are parameters now):
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
```
4. In `stream`, where the block was, put:
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
        yield from self._run_plan(plan, subtask_results, results_by_id, spawn_rewrites)

```

- [ ] **Step 4: Extract the async block**

In `AsyncSupervisor.astream`:

1. **Cut** from (and including) the comment `        # Emit a `dispatch` event for every step up-front so UIs can` through the end of the `finally:` clause, i.e. up to (but excluding) `        yield {"type": "synthesize_start"}`. The block ends with the three lines `await asyncio.gather(*running, return_exceptions=True)` / `running.clear()`.
2. **Paste** it as the body of a new async generator placed immediately before `async def astream`:
```python
    async def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
    ):
        """Execute ``plan`` with the completion-driven scheduler and yield
        ``dispatch`` / ``subtask_result`` events. Results are appended to the
        shared ``subtask_results`` / ``results_by_id`` so several rounds can
        build on one another."""
```
3. In the pasted block, **delete** these two lines (now parameters):
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
```
4. In `astream`, where the block was, put:
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        async for event in self._run_plan(plan, subtask_results, results_by_id):
            yield event

```

- [ ] **Step 5: Prove nothing changed**

Run: `git diff -w --color-moved=dimmed-zebra --stat agentx_dev/Supervisor.py` and skim the diff: the moved block should show as moved, with only the header, the removed container initialisations, and the new call sites as real changes.
Run: `python -m pytest tests/test_supervisor_run_plan.py -q -p no:cacheprovider`
Expected: 2 passed.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures (every existing Supervisor test still passes unchanged).

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_run_plan.py
git commit -m "refactor(supervisor): extract _run_plan so recovery rounds can reuse it"
```

---

### Task 10: Persistent mode in the sync Supervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Test: `tests/test_supervisor_persistent.py`

**Interfaces:**
- Consumes: Task 8 helpers and imports; Task 9 `_run_plan`; `RunBudget`, `apply_persistence`, `accepts_budget`, `Persistence` (Tasks 1, 4); `AgentRunner.Initialize(..., _budget=)` and the `persistence` property (Task 6).
- Produces:
  - `Supervisor(..., persistence: Optional[Persistence] = None)`
  - `Supervisor._run_plan(..., budget: Optional[RunBudget] = None)`, `Supervisor._dispatch_with_retry(..., budget=None)`, `Supervisor._plan_recovery(user_task, results, round_no) -> List[dict]`, `Supervisor._synthesize(user_task, results, unresolved=None)`
  - module functions `_budget_reason`, `_count_done`, `_recovery_note`, `_rename_colliding_ids`, `_unresolved_note`, `_log_replan`; `_sanitize_plan(plan, verbose=False, known_ids=())`; `_topo_order` tolerant of dependencies outside the plan
  - the `replan` event `{"type": "replan", "round": int, "unresolved": [...], "plan": [...]}`
  - behavior per spec 4.7: recovery rounds, shared `RunBudget`, `apply_persistence` for the duration of the run, honest synthesis, cost-limit fallback

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_persistent.py`:

```python
"""Persistent Supervisor: recovery rounds under one shared budget (sync)."""

import json
from types import SimpleNamespace

import pytest

from agentx_dev import CostBudgetExceeded, Persistence
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.Supervisor import (
    SpawnConfig, Supervisor, _rename_colliding_ids, _sanitize_plan, _topo_order,
)
from tests.conftest import MockModel

P = Persistence(max_minutes=5)


def step(id_, agent, query="q", deps=None):
    d = {"id": id_, "agent": agent, "query": query}
    if deps:
        d["depends_on"] = deps
    return d


def plan(*steps):
    return json.dumps({"plan": list(steps)})


class ScriptedRunner:
    """Fake specialist. Each call returns the next (content, outcome)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.tools = []
        self.persistence = None
        self.seen_persistence = []

    def Initialize(self, query, _budget=None):
        self.calls.append((query, _budget))
        self.seen_persistence.append(self.persistence)
        content, outcome = self.results.pop(0) if self.results else ("ok", "done")
        progress = {"failed": ["tool(x) -> boom [rung 1]"]} if outcome != "done" else None
        return SimpleNamespace(content=content, outcome=outcome, output=None, progress=progress)


def supervisor(model, agents, persistence=P, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False,
                      persistence=persistence, **kw)


class TestRecovery:
    def test_replans_a_failing_step_and_finishes_in_round_two(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer", "another way")), "All done."])
        result = supervisor(model, {"worker": worker, "fixer": fixer}).run("task")
        assert result.outcome == "done" and result.content == "All done."
        assert [(s.step_id, s.superseded, s.outcome) for s in result.subtasks] == [
            ("s1", True, "stuck"), ("fix", False, "done")]
        prompt = model.calls[1][0]["content"]
        assert "RECOVERY ROUND 2" in prompt and "[s1] worker" in prompt and "tool(x) -> boom" in prompt
        assert len(worker.calls) == 1 and len(fixer.calls) == 1

    def test_never_reruns_completed_steps_and_a_recovery_step_can_depend_on_them(self):
        a = ScriptedRunner(("A-RESULT", "done"))
        b = ScriptedRunner(("nope", "stuck"))
        fixer = ScriptedRunner(("fixed with A", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")),
                                  plan(step("fix", "fixer", "use A", deps=["s1"])), "Done."])
        result = supervisor(model, {"a": a, "b": b, "fixer": fixer}).run("task")
        assert len(a.calls) == 1
        assert "A-RESULT" in fixer.calls[0][0]
        assert result.outcome == "done"

    def test_a_recovery_step_reusing_an_old_id_gets_a_new_one(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("s1", "fixer")), "Done."])
        result = supervisor(model, {"worker": worker, "fixer": fixer}).run("task")
        assert [s.step_id for s in result.subtasks] == ["s1", "r2_s1"]

    def test_stops_after_max_replans_without_progress(self):
        worker = ScriptedRunner(("x", "stuck"))
        fixer = ScriptedRunner(("y", "stuck"), ("z", "stuck"), ("w", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("f1", "fixer")),
                                  plan(step("f2", "fixer")), "Could not finish."])
        result = supervisor(model, {"worker": worker, "fixer": fixer},
                            persistence=Persistence(max_minutes=5, max_replans=2)).run("task")
        assert len(fixer.calls) == 2 and len(model.calls) == 4
        assert result.outcome == "stuck" and result.content == "Could not finish."

    def test_a_planner_failure_keeps_the_results_and_goes_to_synthesis(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "this is not json", "Partial answer."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert result.content == "Partial answer." and result.outcome == "stuck"
        assert result.subtasks[0].step_id == "s1" and not result.subtasks[0].superseded

    def test_the_synthesis_prompt_names_the_unfinished_steps(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "not json", "Partial."])
        supervisor(model, {"worker": worker}).run("task")
        synth_prompt = model.calls[2][0]["content"]
        assert "did NOT finish" in synth_prompt and "[s1] worker" in synth_prompt

    def test_stream_emits_a_replan_event(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer")), "Done."])
        events = list(supervisor(model, {"worker": worker, "fixer": fixer}).stream("task"))
        replans = [e for e in events if e["type"] == "replan"]
        assert len(replans) == 1 and replans[0]["round"] == 2 and replans[0]["unresolved"] == ["s1"]
        assert [e["type"] for e in events if e["type"] in ("plan", "completion")] == ["plan", "completion"]


class TestSharedBudget:
    def test_every_specialist_gets_the_same_deadline(self):
        a, b = ScriptedRunner(("1", "done")), ScriptedRunner(("2", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")), "ok"])
        supervisor(model, {"a": a, "b": b}).run("task")
        budget_a, budget_b = a.calls[0][1], b.calls[0][1]
        assert isinstance(budget_a, RunBudget) and budget_a is budget_b

    def test_runners_without_their_own_settings_get_the_supervisors_for_the_run_only(self):
        worker = ScriptedRunner(("ok", "done"))
        own = ScriptedRunner(("ok", "done"))
        own.persistence = Persistence(max_minutes=1)
        model = MockModel(script=[plan(step("s1", "worker"), step("s2", "own")), "ok"])
        supervisor(model, {"worker": worker, "own": own}).run("task")
        assert worker.seen_persistence == [P] and worker.persistence is None
        assert own.seen_persistence[0].max_minutes == 1

    def test_a_spent_budget_skips_the_work_and_reports_out_of_time(self, monkeypatch):
        monkeypatch.setattr(RunBudget, "start",
                            classmethod(lambda cls, minutes, clock=None: cls(0.0, lambda: 1.0)))
        worker = ScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert worker.calls == [] and len(model.calls) == 2
        assert result.subtasks[0].outcome == "out_of_time" and result.outcome == "out_of_time"

    def test_a_specialist_that_hit_the_cost_cap_ends_the_run_as_out_of_budget(self):
        worker = ScriptedRunner(("partial", "out_of_budget"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert len(worker.calls) == 1 and len(model.calls) == 2      # no retry, no replan
        assert result.outcome == "out_of_budget"

    def test_a_cost_error_during_synthesis_falls_back_to_the_results(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                return plan(step("s1", "worker"))
            raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)

        result = supervisor(MockModel(script=script), {"worker": ScriptedRunner(("fine", "done"))}).run("task")
        assert result.content.startswith("Stopped: the cost budget was reached") and "A: fine" in result.content


class TestDefaultModeUntouched:
    def test_without_persistence_a_failure_is_not_replanned(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "Final."])
        result = Supervisor(model=model, agents={"worker": ("w", worker)}, verbose=False,
                            max_subtask_retries=0).run("task")
        assert len(model.calls) == 2 and result.outcome == "stuck"
        assert worker.calls[0][1] is None            # no shared budget handed out


class TestSpawnedSpecialists:
    def test_a_spawned_specialist_inherits_persistence(self, tmp_path):
        sup = Supervisor(
            model=MockModel(script=[]), agents={}, verbose=False, persistence=P,
            spawn_config=SpawnConfig(enabled=True, auto_spawn=True, auto_spawn_allowed_caps={"files"},
                                     allowed_paths=[str(tmp_path)]),
        )
        name, _ = sup._handle_spawn({"name": "scout", "description": "reads files", "capabilities": ["files"]})
        assert name == "scout" and sup.agents["scout"].runner.persistence is P


class TestPlanHelpers:
    def test_sanitize_keeps_dependencies_on_earlier_rounds_and_drops_ghosts(self):
        sane, repairs = _sanitize_plan(
            [{"id": "fix", "agent": "x", "query": "q", "depends_on": ["s1", "ghost"]}], known_ids={"s1"})
        assert sane[0]["depends_on"] == ["s1"] and any("ghost" in r for r in repairs)
        assert _topo_order(sane) == [0]

    def test_rename_colliding_ids_rewrites_references_inside_the_plan(self):
        out = _rename_colliding_ids(
            [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a", "s1"]}], {"a", "s1"}, 3)
        assert [s["id"] for s in out] == ["r3_a", "b"] and out[1]["depends_on"] == ["r3_a", "s1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_supervisor_persistent.py -q -p no:cacheprovider`
Expected: FAIL (`unexpected keyword argument 'persistence'`).

- [ ] **Step 3: Plan-graph helpers**

In `agentx_dev/Supervisor.py`:

(a) `_topo_order`: dependencies that point outside the plan (an earlier round's step) are already satisfied. Replace
```python
    indeg = [len(s.get("depends_on") or []) for s in plan]
    dependents: List[List[int]] = [[] for _ in plan]
    for i, step in enumerate(plan):
        for d in step.get("depends_on") or []:
            dependents[id_idx[d]].append(i)
```
with
```python
    # Dependencies outside this plan (a finished step from an earlier recovery
    # round) are already satisfied and are not part of this graph.
    indeg = [sum(1 for d in (s.get("depends_on") or []) if d in id_idx) for s in plan]
    dependents: List[List[int]] = [[] for _ in plan]
    for i, step in enumerate(plan):
        for d in step.get("depends_on") or []:
            if d in id_idx:
                dependents[id_idx[d]].append(i)
```

(b) `_sanitize_plan` accepts `known_ids`. Replace
```python
def _sanitize_plan(plan: List[dict], verbose: bool = False) -> Tuple[List[dict], List[str]]:
```
with
```python
def _sanitize_plan(
    plan: List[dict], verbose: bool = False, known_ids: Iterable[str] = (),
) -> Tuple[List[dict], List[str]]:
```
add `Iterable` to the `typing` import line (`from typing import Callable, Dict, Iterable, List, ...`), and add this paragraph to the docstring's rule list: `known_ids` are step ids finished in earlier recovery rounds; a dependency on one is valid and kept (it is simply not an edge in this plan's graph). Then replace
```python
            elif d not in id_pos:
                repairs.append(f"step {step['id']!r}: unknown dependency {d!r} dropped")
```
with
```python
            elif d not in id_pos and d not in known:
                repairs.append(f"step {step['id']!r}: unknown dependency {d!r} dropped")
```
and, just before the `# -- 2/3/5. dep validation` comment, add `known = set(known_ids)`. Finally replace
```python
        for step in plan:
            for d in step["depends_on"]:
                indeg[step["id"]] += 1
                dependents[d].append(step["id"])
```
with
```python
        for step in plan:
            for d in step["depends_on"]:
                if d not in id_pos:      # an earlier round's step: not part of this graph
                    continue
                indeg[step["id"]] += 1
                dependents[d].append(step["id"])
```

(c) New helpers. Insert after `_supervisor_outcome` (Task 8):
```python
def _budget_reason(budget: Optional[RunBudget], results: List["SubtaskResult"]) -> Optional[str]:
    """Why the run must stop trying: a specialist hit the cost cap, or time is up."""
    if any(r.outcome == OUTCOME_OUT_OF_BUDGET for r in results):
        return OUTCOME_OUT_OF_BUDGET
    if (budget is not None and budget.expired()) or any(r.outcome == OUTCOME_OUT_OF_TIME for r in results):
        return OUTCOME_OUT_OF_TIME
    return None


def _count_done(results: List["SubtaskResult"]) -> int:
    return sum(1 for r in results if _result_done(r))


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
                  "catalog cannot do it. Only reference completed step ids in depends_on."]
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
```

- [ ] **Step 4: `Supervisor` changes**

(a) Constructor. In the signature, after `max_plan_retries: int = 1,` add `persistence: Optional[Persistence] = None,`. Document it: replace
```python
                better. ``0`` disables replanning.
        """
        self.model = model
        # Normalize (and copy)
```
with
```python
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
        # Normalize (and copy)
```
and replace
```python
        self.subtask_success_check = subtask_success_check
        self.max_plan_retries = max(0, int(max_plan_retries))
```
with
```python
        self.subtask_success_check = subtask_success_check
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
```

(b) Spawned specialists inherit it. Replace
```python
        self.agents[req.name] = Specialist(description=description, runner=runner)
```
with
```python
        self.agents[req.name] = Specialist(description=description, runner=runner)
        if self.persistence is not None:
            runner.persistence = self.persistence
```

(c) `_dispatch_with_retry` takes the budget and does not retry once it is spent. Replace the signature
```python
    def _dispatch_with_retry(
        self,
        agent_runner: AgentRunner,
        agent_name: str,
        sub_query: str,
        dispatched_query: str,
    ) -> SubtaskResult:
```
with
```python
    def _dispatch_with_retry(
        self,
        agent_runner: AgentRunner,
        agent_name: str,
        sub_query: str,
        dispatched_query: str,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
```
replace
```python
                completion = agent_runner.Initialize(query)
```
with
```python
                if budget is not None and accepts_budget(agent_runner.Initialize):
                    completion = agent_runner.Initialize(query, _budget=budget)
                else:
                    completion = agent_runner.Initialize(query)
```
and replace
```python
            last_result = result
            last_error = f"did not meet success criteria: {reason}"
```
with
```python
            last_result = result
            last_error = f"did not meet success criteria: {reason}"
            if result.outcome in (OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET):
                break          # the shared budget is spent; another attempt cannot help
```

(d) `_run_plan` takes the budget. Replace its header
```python
        spawn_rewrites: Dict[str, str],
    ):
        """Execute ``plan``: dispatch each step
```
with
```python
        spawn_rewrites: Dict[str, str],
        budget: Optional[RunBudget] = None,
    ):
        """Execute ``plan``: dispatch each step
```
Then replace
```python
            yield {"type": "dispatch", "agent": agent_name, "query": sub_query,
                   "step": step_idx, "step_id": step_id}
```
with
```python
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
```
and replace
```python
            sub_result = self._dispatch_with_retry(
                agent_runner, agent_name, sub_query, dispatched_query,
            )
```
with
```python
            sub_result = self._dispatch_with_retry(
                agent_runner, agent_name, sub_query, dispatched_query, budget,
            )
```

(e) Recovery planning. Add this method after `_plan`:
```python
    def _plan_recovery(self, user_task: str, results: List[SubtaskResult], round_no: int) -> List[dict]:
        """Ask the planner for a recovery plan covering only the unfinished
        work. Returns ``[]`` when it cannot (the caller then synthesizes with
        what it has)."""
        try:
            raw = self._plan_once(user_task, repair_note=_recovery_note(results, round_no))
        except Exception as e:
            logger.warning(f"recovery planning failed: {e}")
            return []
        if not raw:
            return []
        known = {r.step_id for r in results
                 if r.step_id and r.agent != "__spawn__" and not _result_failed(r) and not r.superseded}
        sane, _ = _sanitize_plan(raw, verbose=self.verbose, known_ids=known)
        prior = {r.step_id for r in results if r.step_id}
        return _rename_colliding_ids(sane, prior, round_no)
```

(f) Honest synthesis with a cost-limit fallback. Replace the whole sync `_synthesize` method (the one whose signature is `def _synthesize(self, user_task: str, subtask_results: List[SubtaskResult]) -> str:`) with
```python
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
            return self.model.Initialize(messages=messages)
        except CostBudgetExceeded:
            if self.persistence is None:
                raise
            return f"Stopped: the cost budget was reached.\n\n{results_block}"
```

(g) `stream`: the shared budget at the start. Replace
```python
        self._spawns_this_run = 0

        yield {"type": "plan_start"}
        plan = self._plan(user_task)
```
with
```python
        self._spawns_this_run = 0
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None

        yield {"type": "plan_start"}
        plan = self._plan(user_task)
```

(h) `stream`: the recovery loop and the result. Replace the region that Task 9 left at the end of `stream`, i.e.
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
        yield from self._run_plan(plan, subtask_results, results_by_id, spawn_rewrites)

        yield {"type": "synthesize_start"}
        final = self._synthesize(user_task, subtask_results)
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
            outcome=_supervisor_outcome(subtask_results),
        )
```
with
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        spawn_rewrites: Dict[str, str] = {}
        budget_reason: Optional[str] = None
        with apply_persistence([s.runner for s in self.agents.values()], self.persistence):
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
                    recovery = self._plan_recovery(user_task, subtask_results, round_no + 1)
                    if not recovery:
                        break
                    round_no += 1
                    for r in unresolved:
                        r.superseded = True
                    done_before = _count_done(subtask_results)
                    yield {"type": "replan", "round": round_no,
                           "unresolved": [r.step_id for r in unresolved], "plan": recovery}
                    if self.verbose:
                        _log_replan(round_no, unresolved)
                    plan.extend(recovery)
                    yield from self._run_plan(recovery, subtask_results, results_by_id,
                                              spawn_rewrites, budget)
                    stagnant = 0 if _count_done(subtask_results) > done_before else stagnant + 1

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
        )
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `python -m pytest tests/test_supervisor_persistent.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_persistent.py
git commit -m "feat(supervisor): persistent mode -- replan unfinished steps under one shared budget"
```

---

### Task 11: Persistent mode in the async Supervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Test: `tests/test_supervisor_persistent_async.py`

**Interfaces:**
- Consumes: everything from Task 10 (module helpers, `_plan_recovery` logic); `AsyncAgentRunner.Initialize(..., _budget=)` (Task 7).
- Produces: `AsyncSupervisor(..., persistence=None)`; `AsyncSupervisor._run_plan(plan, subtask_results, results_by_id, budget=None)`; `AsyncSupervisor._run_subtask(agent_name, sub_query, prior_results=None, budget=None)`; `AsyncSupervisor._plan_recovery(user_task, results, round_no)` (async); `AsyncSupervisor._synthesize(user_task, results, unresolved=None)`; the `replan` event on `astream`. The async supervisor has no `spawn_config`, so recovery plans cannot spawn.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_persistent_async.py`:

```python
"""Persistent AsyncSupervisor: the sync scenarios on the async scheduler."""

import asyncio
import json

from agentx_dev import CostBudgetExceeded, Persistence
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.Supervisor import AsyncSupervisor
from tests.conftest import MockModel
from tests.test_supervisor_persistent import ScriptedRunner, plan, step

P = Persistence(max_minutes=5)


class AsyncScriptedRunner(ScriptedRunner):
    async def Initialize(self, query, _budget=None):          # type: ignore[override]
        return ScriptedRunner.Initialize(self, query, _budget)


def supervisor(model, agents, persistence=P, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False,
                           persistence=persistence, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


class TestRecovery:
    def test_replans_a_failing_step_and_finishes_in_round_two(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        fixer = AsyncScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer", "another way")), "All done."])
        result = run(supervisor(model, {"worker": worker, "fixer": fixer}))
        assert result.outcome == "done" and result.content == "All done."
        assert [(s.step_id, s.superseded) for s in result.subtasks] == [("s1", True), ("fix", False)]
        assert "RECOVERY ROUND 2" in model.calls[1][0]["content"]
        assert len(worker.calls) == 1 and len(fixer.calls) == 1

    def test_never_reruns_completed_steps(self):
        a = AsyncScriptedRunner(("A-RESULT", "done"))
        b = AsyncScriptedRunner(("nope", "stuck"))
        fixer = AsyncScriptedRunner(("fixed with A", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")),
                                  plan(step("fix", "fixer", "use A", deps=["s1"])), "Done."])
        result = run(supervisor(model, {"a": a, "b": b, "fixer": fixer}))
        assert len(a.calls) == 1 and "A-RESULT" in fixer.calls[0][0] and result.outcome == "done"

    def test_stops_after_max_replans_without_progress(self):
        worker = AsyncScriptedRunner(("x", "stuck"))
        fixer = AsyncScriptedRunner(("y", "stuck"), ("z", "stuck"), ("w", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("f1", "fixer")),
                                  plan(step("f2", "fixer")), "Could not finish."])
        result = run(supervisor(model, {"worker": worker, "fixer": fixer},
                                persistence=Persistence(max_minutes=5, max_replans=2)))
        assert len(fixer.calls) == 2 and len(model.calls) == 4 and result.outcome == "stuck"

    def test_a_planner_failure_keeps_the_results(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "this is not json", "Partial answer."])
        result = run(supervisor(model, {"worker": worker}))
        assert result.content == "Partial answer." and not result.subtasks[0].superseded

    def test_astream_emits_a_replan_event(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        fixer = AsyncScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer")), "Done."])

        async def collect():
            return [e async for e in supervisor(model, {"worker": worker, "fixer": fixer}).astream("task")]

        events = asyncio.run(collect())
        replans = [e for e in events if e["type"] == "replan"]
        assert len(replans) == 1 and replans[0]["round"] == 2 and replans[0]["unresolved"] == ["s1"]


class TestSharedBudget:
    def test_every_specialist_gets_the_same_deadline(self):
        a, b = AsyncScriptedRunner(("1", "done")), AsyncScriptedRunner(("2", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")), "ok"])
        run(supervisor(model, {"a": a, "b": b}))
        assert isinstance(a.calls[0][1], RunBudget) and a.calls[0][1] is b.calls[0][1]

    def test_persistence_is_applied_and_restored(self):
        worker = AsyncScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "ok"])
        run(supervisor(model, {"worker": worker}))
        assert worker.seen_persistence == [P] and worker.persistence is None

    def test_a_spent_budget_skips_the_work(self, monkeypatch):
        monkeypatch.setattr(RunBudget, "start",
                            classmethod(lambda cls, minutes, clock=None: cls(0.0, lambda: 1.0)))
        worker = AsyncScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = run(supervisor(model, {"worker": worker}))
        assert worker.calls == [] and len(model.calls) == 2
        assert result.outcome == "out_of_time" and result.subtasks[0].outcome == "out_of_time"

    def test_a_specialist_that_hit_the_cost_cap_ends_the_run(self):
        worker = AsyncScriptedRunner(("partial", "out_of_budget"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = run(supervisor(model, {"worker": worker}))
        assert len(worker.calls) == 1 and len(model.calls) == 2 and result.outcome == "out_of_budget"

    def test_a_cost_error_during_synthesis_falls_back_to_the_results(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                return plan(step("s1", "worker"))
            raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)

        result = run(supervisor(MockModel(script=script), {"worker": AsyncScriptedRunner(("fine", "done"))}))
        assert result.content.startswith("Stopped: the cost budget was reached") and "A: fine" in result.content


def test_without_persistence_a_failure_is_not_replanned():
    worker = AsyncScriptedRunner(("gave up", "stuck"))
    model = MockModel(script=[plan(step("s1", "worker")), "Final."])
    result = asyncio.run(AsyncSupervisor(model=model, agents={"worker": ("w", worker)}, verbose=False,
                                         max_subtask_retries=0).run("task"))
    assert len(model.calls) == 2 and result.outcome == "stuck" and worker.calls[0][1] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_supervisor_persistent_async.py -q -p no:cacheprovider`
Expected: FAIL (`unexpected keyword argument 'persistence'`).

- [ ] **Step 3: `AsyncSupervisor` changes**

(a) Constructor. After `max_plan_retries: int = 1,` in the signature add `persistence: Optional[Persistence] = None,`. Document it: replace
```python
            max_plan_retries: (3.3) Replans after sanitization repairs;
                see :class:`Supervisor`.
        """
```
with
```python
            max_plan_retries: (3.3) Replans after sanitization repairs;
                see :class:`Supervisor`.
            persistence: (3.5) Opt-in ``Persistence(...)``; see
                :class:`Supervisor`. Recovery plans cannot spawn new
                specialists here (the async supervisor has no
                ``spawn_config``).
        """
```
and add `self.persistence = persistence` right after the `self.max_plan_retries = ...` line. That line is not unique, so edit it with its predecessor: replace
```python
        )
        self.max_plan_retries = max(0, int(max_plan_retries))
```
with
```python
        )
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
```

(b) `_run_subtask` takes the budget. Replace its signature
```python
    async def _run_subtask(
        self,
        agent_name: str,
        sub_query: str,
        prior_results: Optional[List[SubtaskResult]] = None,
    ) -> SubtaskResult:
```
with
```python
    async def _run_subtask(
        self,
        agent_name: str,
        sub_query: str,
        prior_results: Optional[List[SubtaskResult]] = None,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
```
replace
```python
                if asyncio.iscoroutinefunction(initialize):
                    completion = await initialize(query)
                else:
                    completion = await asyncio.to_thread(initialize, query)
```
with
```python
                budget_kw = {"_budget": budget} if budget is not None and accepts_budget(initialize) else {}
                if asyncio.iscoroutinefunction(initialize):
                    completion = await initialize(query, **budget_kw)
                else:
                    completion = await asyncio.to_thread(initialize, query, **budget_kw)
```
and replace
```python
            last_result = candidate
            last_error = f"did not meet success criteria: {reason}"
```
with
```python
            last_result = candidate
            last_error = f"did not meet success criteria: {reason}"
            if candidate.outcome in (OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET):
                break          # the shared budget is spent; another attempt cannot help
```

(c) `_run_plan` takes the budget. Replace its signature
```python
    async def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
    ):
```
with
```python
    async def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        budget: Optional[RunBudget] = None,
    ):
```
In the scheduler's launch loop, replace
```python
                    if self.max_parallel is not None and len(running) >= self.max_parallel:
                        break
                    launched.add(i)
```
with
```python
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
```
and replace
```python
                        prior_results=dep_results if dep_results else None,
                    ))
```
with
```python
                        prior_results=dep_results if dep_results else None,
                        budget=budget,
                    ))
```

(d) Recovery planning and honest synthesis. Add after `_plan`:
```python
    async def _plan_recovery(self, user_task: str, results: List[SubtaskResult], round_no: int) -> List[dict]:
        """Async twin of :meth:`Supervisor._plan_recovery`."""
        try:
            raw = await self._plan_once(user_task, repair_note=_recovery_note(results, round_no))
        except Exception as e:
            logger.warning(f"recovery planning failed: {e}")
            return []
        if not raw:
            return []
        known = {r.step_id for r in results
                 if r.step_id and r.agent != "__spawn__" and not _result_failed(r) and not r.superseded}
        sane, _ = _sanitize_plan(raw, verbose=self.verbose, known_ids=known)
        prior = {r.step_id for r in results if r.step_id}
        return _rename_colliding_ids(sane, prior, round_no)
```
Replace the whole async `_synthesize` (signature `async def _synthesize(\n        self, user_task: str, subtask_results: List[SubtaskResult]\n    ) -> str:`) with
```python
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
        except CostBudgetExceeded:
            if self.persistence is None:
                raise
            return f"Stopped: the cost budget was reached.\n\n{results_block}"
```

(e) `astream`: the budget, the recovery loop and the result. Replace
```python
        yield {"type": "plan_start"}
        plan = await self._plan(user_task)
```
with
```python
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
        yield {"type": "plan_start"}
        plan = await self._plan(user_task)
```
and replace the region Task 9 left at the end of `astream`, i.e.
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        async for event in self._run_plan(plan, subtask_results, results_by_id):
            yield event

        yield {"type": "synthesize_start"}
        final = await self._synthesize(user_task, list(subtask_results))
        if self.verbose:
            _log_final(final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
            outcome=_supervisor_outcome(subtask_results),
        )
```
with
```python
        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        budget_reason: Optional[str] = None
        with apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            async for event in self._run_plan(plan, subtask_results, results_by_id, budget):
                yield event
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
                    recovery = await self._plan_recovery(user_task, subtask_results, round_no + 1)
                    if not recovery:
                        break
                    round_no += 1
                    for r in unresolved:
                        r.superseded = True
                    done_before = _count_done(subtask_results)
                    yield {"type": "replan", "round": round_no,
                           "unresolved": [r.step_id for r in unresolved], "plan": recovery}
                    if self.verbose:
                        _log_replan(round_no, unresolved)
                    plan.extend(recovery)
                    async for event in self._run_plan(recovery, subtask_results, results_by_id, budget):
                        yield event
                    stagnant = 0 if _count_done(subtask_results) > done_before else stagnant + 1

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
        )
```

- [ ] **Step 4: Run the tests and the full suite**

Run: `python -m pytest tests/test_supervisor_persistent_async.py -q -p no:cacheprovider`
Expected: all pass.
Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_persistent_async.py
git commit -m "feat(supervisor): persistent mode in the async supervisor"
```

---

### Task 12: Long-run check, documentation, and the 3.5.0 version

**Files:**
- Create: `tests/test_persistent_soak.py`
- Create: `docs/guides/long-running-agents.md`
- Modify: `host/build_data.py`, `host/app.js`, `README.md`, `docs/cookbook/patterns.md`, `docs/cookbook/faq.md`, `docs/cookbook/troubleshooting.md`, `docs/guides/upgrading.md`, `docs/reference/api-summary.md`, `CHANGELOG.md`, `pyproject.toml`, `host/data.js` (regenerated)

**Interfaces:**
- Consumes: everything from Tasks 1-11. The cost cap API is `model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)` (both prices required; cumulative per model object). Do not invent a `cost_budget_usd` constructor argument: it does not exist.
- Produces: the shipped, documented 3.5.0 tree (not yet released; Task 13).

- [ ] **Step 1: Write the long-run check**

Create `tests/test_persistent_soak.py`:

```python
"""A long persistent run keeps its context bounded."""

from agentx_dev import AgentRunner, AgentType, Persistence, StandardTool
from agentx_dev.Runner.Persistence import estimate_tokens
from tests.conftest import MockModel, make_final, make_react_response

TURNS = 3000


def test_context_stays_bounded_over_thousands_of_turns():
    sizes = []
    summaries = []
    turn = []

    def script(messages):
        if len(messages) == 1 and "You are compacting" in str(messages[0]["content"]):
            summaries.append(1)
            return "NOTES"
        turn.append(1)
        sizes.append(estimate_tokens(messages))
        if len(turn) < TURNS:
            return make_react_response("big", str(len(turn)))
        return make_final("finished")

    big = StandardTool(func=lambda x: f"call {x}: " + "line of output " * 40, name="big", description="big output")
    cfg = Persistence(max_minutes=5, compact_at_tokens=3000, keep_recent_turns=6, max_turns=TURNS + 10)
    runner = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[big],
                         verbose=False, persistence=cfg)
    result = runner.invoke("go")

    assert result.outcome == "done" and result.content == "finished"
    assert len(sizes) == TURNS
    # Each call returns different text only to keep the history realistic; identical results are not a stuck signal.
    # Without compaction the history would reach hundreds of thousands of tokens.
    assert max(sizes) < 20_000, max(sizes)
    assert len(summaries) > 10
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_persistent_soak.py -q -p no:cacheprovider`
Expected: 1 passed, in a few seconds. If `max(sizes)` is over the bound, print `sizes[::200]`: a steadily growing series means compaction is not shrinking the history (investigate `plan_compaction`), while a sawtooth just above 20,000 means the system prompt is larger than assumed (raise the bound, not the threshold).

- [ ] **Step 3: Create `docs/guides/long-running-agents.md`**

````markdown
# Long-running agents *(3.5)*

By default an agent stops when its step budget runs out (`max_iterations`,
4 unless you raise it) or when it repeats the same call, and a
`Supervisor` plans once and moves past a failed step. That's the right
default for short tasks.

For work that can take a while — getting a failing build to pass,
migrating a folder of files, researching a question with dead ends —
turn on **persistent mode**. The agent notices when it's stuck, changes
approach, and stops only when it has finished or a time or cost limit
is reached.

## Turn it on

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Persistence

model = GPT(model="gpt-5.4").configure_limits(
    budget_usd=5.00,
    input_price_per_1k=0.0025,       # your provider's prices
    output_price_per_1k=0.01,
)

runner = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    persistence=Persistence(max_minutes=60),
)

result = runner.invoke("Get the failing tests in ./workspace passing")
print(result.outcome)       # "done", or why it stopped
print(result.content)       # the answer, or a report of where it stopped
```

`persistence=None` (the default) keeps the ordinary loop. Set a cost
cap whenever you use persistence: the time limit alone doesn't stop a
fast, expensive loop.

## What comes back

Every run — persistent or not — reports how it ended:

| `result.outcome` | Meaning |
|---|---|
| `done` | The agent returned a final answer. |
| `stuck` | It tried the recovery steps below and was still stuck. |
| `out_of_time` | `max_minutes` passed. |
| `out_of_budget` | The model's cost cap was reached. |
| `iteration_limit` | A step cap was reached (`max_iterations`, or `max_turns` in persistent mode). |

When a run doesn't finish, `result.content` is a report, not an answer:
why it stopped, what it completed, what failed, and what it planned
next. `result.progress` has the same facts as data
(`goal` / `done` / `failed` / `next`). Nothing is raised for a time or
cost limit.

## How it recovers

The loop watches for two kinds of trouble, each counted over
`reflect_after` turns in a row (default 3): the same call repeated,
or tool errors. When one fires it doesn't give up.
It adds a note to the last tool result and carries on, a little firmer
each time:

1. Name the root cause in one sentence, and don't repeat the call that
   failed.
2. Choose an approach you haven't tried. The failed attempts are listed.
3. Check your assumptions with one cheap read-only probe before another
   write or retry.
4. If you're still blocked, say exactly what's blocking you.

If the trouble comes back after the last step, the run ends `stuck` with
the report above. Any real progress — a successful call that isn't a
repeat — resets the ladder, so a run can work for hours: each stuck
patch escalates, then starts over once the agent is moving again.

## Long runs and the context window

Hours of tool output would overflow any context window. Once the
history passes `compact_at_tokens` (default 60,000, estimated), the
turns in the middle are replaced by a short set of notes: one extra
model call summarizes what was learned and what's left, and the list of
failed attempts is kept word for word. The task itself (including any
images or files you attached) and the most recent turns
(`keep_recent_turns`, default 6) are never touched. If the summary call
fails, the failed-attempts list is used on its own.

## Time, cost, and provider errors

- **Time:** `max_minutes` (default 30) for the whole run. A Supervisor
  and all its specialists share one deadline.
- **Cost:** the model's own cap, set with
  `model.configure_limits(budget_usd=..., input_price_per_1k=...,
  output_price_per_1k=...)`. It counts everything that model object has
  spent, not just this run: use a fresh model object for a per-run cap.
- **Transient provider errors** (HTTP 429 and 5xx, timeouts,
  connection errors) are retried with growing waits until the deadline.
  Errors that won't fix themselves — a bad API key, an invalid request,
  a bug in your tool — still raise straight away. A run with
  `stream_tokens=True` isn't retried mid-response.

## Supervisors

```python
from agentx_dev import Supervisor, Persistence

sup = Supervisor(
    model=planner_model,
    agents={"coder": ("writes and runs code", coder), "writer": ("writes docs", writer)},
    persistence=Persistence(max_minutes=45),
)
result = sup.run("Fix the failing test, then update the changelog")
print(result.outcome)       # done | partial | stuck | out_of_time | out_of_budget
```

When a step doesn't finish, the Supervisor asks the planner for a
recovery plan: steps that did finish are kept (never re-run) and can be
used as inputs; the failed approach is described so it isn't repeated;
the planner may pick a different specialist or, if you enabled
spawning, create one. It keeps going while rounds make progress and
stops after `max_replans` (default 3) rounds in a row that don't. The
final answer says plainly which parts weren't completed.

`Persistence` is applied to specialist runners that don't have their
own, for the duration of the run. `SupervisorResult.outcome` is
computed in default mode too (`done`, `partial` or `stuck`).

## Watch it work

`runner.stream(...)` and `supervisor.stream(...)` add events:

| Event | When |
|---|---|
| `{"type": "reflect", "rung": 2, "reason": "..."}` | A recovery step was added. |
| `{"type": "compact", "before_tokens": ..., "after_tokens": ...}` | History was compacted. |
| `{"type": "replan", "round": 2, "unresolved": [...], "plan": [...]}` | A Supervisor started a recovery round. |

With `verbose=True` the same moments print as `[persist]` lines. The
async runner has no event stream, but `result.progress` and the log
lines are the same.

## Settings

| `Persistence(...)` | Default | What it does |
|---|---|---|
| `max_minutes` | `30` | Wall-clock limit for the run. |
| `reflect_after` | `3` | Bad turns in a row before a recovery step. |
| `max_reflections` | `4` | Recovery steps before the run ends `stuck`. |
| `compact_at_tokens` | `60000` | History size that triggers compaction. |
| `keep_recent_turns` | `6` | Messages kept word for word when compacting. |
| `max_replans` | `3` | Supervisor recovery rounds without progress. |
| `max_turns` | `1000` | Backstop on total model turns. |
| `patient_retries` | `True` | Wait out transient provider errors. |

## Things to know

- **The tool-result cache is off while persistence is set.** A retry
  answered from the cache isn't a retry, and a cached "write succeeded"
  can be false.
- **Progress isn't saved to disk.** If the process dies, the run is
  lost; resuming isn't supported yet.
- **A run can still be wrong.** Persistence keeps an agent working; it
  doesn't check the work. Give the task a way to verify itself (run the
  tests, read the file back).
- **Upgrading:** with or without persistence, a Supervisor now retries
  a specialist that gave up and flags it if it still doesn't finish.
  See [Upgrading](upgrading.md).
````

- [ ] **Step 4: Register the page in the docs site**

In `host/build_data.py`, in the `Guides` group, after the `("guides/sessions", ...)` line add:
```python
        ("guides/long-running-agents", DOCS_DIR / "guides" / "long-running-agents.md", "Long-running agents"),
```

- [ ] **Step 5: Site overview card**

In `host/app.js`, replace
```html
      <h2 class="section-title">What's new in 3.4</h2>
      <ul class="whats-new-list">
```
with
```html
      <h2 class="section-title">What's new in 3.5</h2>
      <ul class="whats-new-list">
        <li>
          <strong>Agents that keep working</strong>
          <div class="desc"><code>persistence=Persistence(max_minutes=60)</code> on a runner or Supervisor: it reflects and changes approach when stuck, compacts long histories, waits out transient provider errors, and stops only when done or out of time or budget. Every run now reports an <code>outcome</code>. <a href="#guides/long-running-agents">Guide</a>.</div>
        </li>
```

- [ ] **Step 6: README**

In `README.md`, insert immediately before `## What's new in 3.4 — media input and models that adapt`:

````markdown
## What's new in 3.5 — agents that keep working

Long jobs no longer end at the first wall. Turn on persistent mode and
an agent that gets stuck changes approach, a long history is compacted
instead of overflowing, and a run stops when it's done or out of time
or budget — with an honest report either way.

```python
from agentx_dev import AgentRunner, AgentType, Persistence

runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=tools,
                     persistence=Persistence(max_minutes=60))
result = runner.invoke("Get the failing tests passing")
print(result.outcome)       # done | stuck | out_of_time | out_of_budget | iteration_limit
```

| Feature | What you get |
|---|---|
| **Recover, don't quit** | After a few failing or repeated turns the agent is asked to name the root cause, then to try a different approach, then to probe its assumptions, before it's allowed to give up. Real progress resets the ladder. |
| **Honest outcomes** | Every run reports `outcome`. A specialist that gave up is no longer reported as a success. |
| **Long histories** | Old turns are summarized into notes; the task, attached files and recent turns stay as they were. |
| **Supervisors that replan** | A failed step is replanned around, completed steps are kept, and every specialist shares one deadline. |
| **Limits** | `max_minutes` plus the model's cost cap (`configure_limits(budget_usd=...)`). Transient provider errors (429, 5xx, timeouts) are retried until the deadline. |

[Guide](docs/guides/long-running-agents.md).

### Upgrade notes

- Persistence is opt-in; nothing changes unless you pass `persistence=`.
- A `Supervisor` now treats a specialist that ended with an outcome
  other than `done` (for example `iteration_limit`) as a failed attempt:
  it retries once with feedback, then flags the step instead of
  reporting success.
- While persistence is set, the tool-result cache is off.

Full notes: [docs/guides/upgrading.md](docs/guides/upgrading.md).

````

- [ ] **Step 7: Cookbook, FAQ, troubleshooting, upgrading, API summary**

(a) `docs/cookbook/patterns.md`: append to the end of the file (pattern 27 is the last one):

````markdown

---

## 28. A fixer that works through failures *(3.5)*

A task where the first attempts usually fail: run the tests, read the
failure, change the code, run again. Persistent mode lets the agent
keep going, and tells you honestly how it ended.

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Persistence

model = GPT(model="gpt-5.4").configure_limits(
    budget_usd=3.00, input_price_per_1k=0.0025, output_price_per_1k=0.01)

fixer = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./project"]),
    system_addendum="Run the tests with run_python (subprocess + pytest). "
                    "Do not say you are done until the tests pass.",
    persistence=Persistence(max_minutes=30),
)

result = fixer.invoke("Make the tests in ./project pass")
if result.outcome == "done":
    print(result.content)
else:
    print(f"Stopped ({result.outcome}):")
    print(result.content)              # what was done, what failed, what was next
    print(result.progress["failed"])   # the failed attempts, as data
```

Things to know:

- **Tell it how to check its own work.** Persistence keeps the agent
  going; the instruction to run the tests is what makes "done" mean
  something.
- **A stuck run is still useful.** The report lists what was tried, so
  you can fix the task or the tools and run it again.
- **Watch it:** `for event in fixer.stream(task)` includes `reflect`
  events when the agent changes approach.

````

(b) `docs/cookbook/faq.md`: insert immediately before `## When should I use ReAct vs. function-calling vs. native binding?`:

````markdown
## My agent gives up after a few steps. How do I make it keep going? *(3.5)*

Turn on persistent mode: `AgentRunner(..., persistence=Persistence(max_minutes=60))`,
or pass `persistence=` to a `Supervisor`. The agent then recovers when
it's stuck (it's asked to find the root cause and try something
different) instead of stopping, and runs until it's done or a time or
cost limit is reached. Set a cost cap with
`model.configure_limits(budget_usd=..., input_price_per_1k=...,
output_price_per_1k=...)` too. See
[Long-running agents](../guides/long-running-agents.md).

````

(c) `docs/cookbook/troubleshooting.md`: insert immediately before `## Tool errors`:

````markdown
## Long runs and stuck agents *(3.5)*

**`result.outcome == "stuck"`**
The agent tried every recovery step and the same trouble came back.
`result.content` says what it was; `result.progress["failed"]` lists the
failed calls. Usually the task or a tool needs changing (a missing
permission, an unclear instruction). Raise `max_reflections` only if the
failed attempts show it was making different attempts each time.

**`result.outcome == "out_of_time"`**
`max_minutes` passed. Raise it, or split the task. The report shows how
far it got.

**`result.outcome == "out_of_budget"`**
The model's cost cap was reached. The cap is cumulative per model object
(everything that object has spent, not just this run), so a model reused
across runs may be nearly spent before the run starts. Use a fresh model
object, or raise `budget_usd`.

**A Supervisor step ends `iteration_limit` or `stuck`**
Since 3.5 a specialist that gave up is retried once with feedback (see
`max_subtask_retries`) and then flagged in `result.subtasks[i].error`
instead of being reported as a success. To make the Supervisor replan
around it instead, pass `persistence=Persistence(...)`.

**My tool ran once, but the agent called it twice**
That's persistent mode doing its job: the tool-result cache is off while
`persistence` is set, so every call executes.

````

(d) `docs/guides/upgrading.md`: insert immediately before `## Upgrading to 3.4.3`:

````markdown
## Upgrading to 3.5.0

**Nothing breaks, with one behavior change.** Persistent mode is opt-in.

- **New:** `Persistence(...)` on `AgentRunner`, `AsyncAgentRunner`,
  `Supervisor` and `AsyncSupervisor`. See
  [Long-running agents](long-running-agents.md).
- **New:** every completion has `.outcome` (`done`, `stuck`,
  `out_of_time`, `out_of_budget`, `iteration_limit`) and `.progress`
  (set only by persistent runs). `SubtaskResult` and `SupervisorResult`
  have `.outcome` too.
- **Changed, even without `persistence`:** a `Supervisor` used to accept
  a specialist that gave up ("Hit max_iterations ...", "Terminated: ...")
  as a normal answer. It now treats any outcome other than `done` as a
  failed attempt: it retries up to `max_subtask_retries` (default 1) with
  the reason fed back, then returns the step with `error` set. If a test
  of yours expected the old behavior, that is why.
- **While persistence is set**, the runner's tool-result cache is off
  and the iteration cap is `max_turns` (default 1000); clearing
  `runner.persistence` restores both.

---

````

(e) `docs/reference/api-summary.md`: in the `Agent runners` table, insert a row after the `AsyncAgentRunner` row:
```
| `Persistence` *(3.5)* | dataclass | Opt-in settings for runs that keep working through errors: `max_minutes`, `reflect_after`, `max_reflections`, `compact_at_tokens`, `keep_recent_turns`, `max_replans`, `max_turns`, `patient_retries`. Pass as `persistence=` to a runner or Supervisor |
```
change the `AgentCompletion` row's text `Result of `runner.invoke`` to ``Result of `runner.invoke` (3.5: `.outcome`, `.progress`)``; and in `Multi-agent orchestration` change the `SupervisorResult` row's text `Plan + subtask results + final` to `Plan + subtask results + final (3.5: `.outcome`)` and append ` (3.5: `persistence=` replans unfinished steps under one shared deadline)` to the end of the `Supervisor` row's description.

- [ ] **Step 8: CHANGELOG and version**

In `CHANGELOG.md`, insert immediately before `## [3.4.3] - 2026-09-26`:

```markdown
## [3.5.0] - 2026-10-04

Agents that keep working through errors. Opt-in; one default change (below).

### Added

- **Persistent mode** (`persistence=Persistence(...)`) for `AgentRunner`,
  `AsyncAgentRunner`, `Supervisor` and `AsyncSupervisor`. When a run gets
  stuck (repeated calls, repeated tool errors, repeated results) it is
  asked, in four escalating steps, to find the root cause, try a
  different approach, probe its assumptions, and say what blocks it,
  before it may stop; real progress resets the ladder. Runs stop on
  `max_minutes` or the model's cost cap, not a step count.
- **Context compaction** for long runs: old turns become notes, the task
  (with its attachments) and recent turns are untouched, and the failed
  attempts are kept word for word.
- **Patient retries**: transient provider errors (429, 5xx, timeouts,
  connection errors) are retried with backoff until the deadline; auth
  and invalid-request errors still raise at once.
- **Supervisors replan**: a step that does not finish is replanned
  around under one shared deadline; finished steps are kept and can be
  depended on. New `replan`, `reflect` and `compact` stream events.
- **Outcomes**: every completion has `.outcome` and (persistent runs)
  `.progress`; `SubtaskResult` and `SupervisorResult` have `.outcome`.
- New guide: Long-running agents.

### Changed

- A `Supervisor`/`AsyncSupervisor` now treats a specialist that finished
  with an outcome other than `done` (for example `iteration_limit`) as a
  failed attempt: it retries up to `max_subtask_retries` with the reason
  fed back, then flags the step. Previously such a result was accepted
  as a success. This applies without `persistence`.
- While `persistence` is set the tool-result cache is off, and the
  iteration cap is `persistence.max_turns`.

```

In `pyproject.toml`, change `version = "3.4.3"` to `version = "3.5.0"` (if a later patch such as 3.4.4 has been released in the meantime, 3.5.0 is still the right number: it is a minor release).

- [ ] **Step 9: Regenerate the site data and verify the docs**

Run: `python host/build_data.py`
Expected: no error; `grep -m1 AGENTX_VERSION host/data.js` prints `window.AGENTX_VERSION = "3.5.0";`.

Run this check that every Python block in the new guide parses and every `agentx_dev` name it imports exists:
```bash
python - <<'PY'
import ast, importlib, pathlib, re
text = pathlib.Path("docs/guides/long-running-agents.md").read_text(encoding="utf-8")
blocks = re.findall(r"```python\n(.*?)```", text, re.S)
for block in blocks:
    for node in ast.walk(ast.parse(block)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agentx_dev"):
            mod = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(mod, alias.name), f"{node.module} has no {alias.name}"
print(len(blocks), "python blocks OK")
PY
```
Expected: `2 python blocks OK`. Also confirm the cost-cap example matches the real signature: `python -c "import inspect, agentx_dev; print(inspect.signature(agentx_dev.GPT.configure_limits))"` shows `budget_usd`, `input_price_per_1k`, `output_price_per_1k`.

- [ ] **Step 10: Full suite**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: every new test plus the original `309 passed, 4 skipped`, no failures.

- [ ] **Step 11: Commit**

```bash
git add tests/test_persistent_soak.py docs/guides/long-running-agents.md docs/cookbook docs/guides/upgrading.md docs/reference/api-summary.md README.md CHANGELOG.md pyproject.toml host/build_data.py host/app.js host/data.js
git commit -m "docs: long-running agents guide, cookbook, upgrade notes; 3.5.0"
```

---

### Task 13: Release checklist (do not run without the user's go-ahead)

Releasing publishes to PyPI and GitHub, which cannot be undone. The user approves each release explicitly; this task is the checklist, not an instruction to execute. It is the same pipeline used for 3.4.x.

- [ ] Re-read the spec's "Risks" section and the open question in the final review: has the separate tool-cache fix landed or been released? If the user wants it in the same release, merge that branch first and re-run the full suite.
- [ ] Confirm the CHANGELOG date for `[3.5.0]` is the release day (edit it if not), then re-run `python host/build_data.py`.
- [ ] Full suite green: `python -m pytest tests -q -p no:cacheprovider`.
- [ ] Sync the `PIP version` folder from the worktree: mirror `agentx_dev/`, copy `README.md`, `CHANGELOG.md`, `pyproject.toml`, `LICENSE`, `MANIFEST.in`, `AGENTX.md`, `CONTRIBUTING.md`.
- [ ] Build there (`python -m build`), `python -m twine check dist/agentx_dev-3.5.0*`, and verify the wheel contains `agentx_dev/Runner/Persistence.py` and that `Persistence` imports from an isolated `pip install --no-deps --target <dir>` copy; run the persistence tests against that copy.
- [ ] Push `main` and the annotated tag `v3.5.0` (no `Co-Authored-By` trailer anywhere), upload with `python -m twine upload --config-file <pypirc.ini> dist/agentx_dev-3.5.0*`, and verify `https://pypi.org/pypi/agentx-dev/3.5.0/json`.
- [ ] Fast-forward the main checkout (`git -C <main checkout> pull --ff-only origin main`) after confirming it is clean.
- [ ] With the user's permission, upgrade their local install (`python -m pip install -U agentx-dev`) and remind them to restart any running kernel.
