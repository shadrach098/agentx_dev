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
