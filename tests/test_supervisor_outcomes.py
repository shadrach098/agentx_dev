"""A specialist that gave up is a failed attempt, not a success (default mode)."""

import asyncio
import json
from types import SimpleNamespace

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Supervisor import (
    AsyncSupervisor, Supervisor, SubtaskResult, _result_done, _result_failed, _supervisor_outcome,
)
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


def res(agent="a", **kw):
    return SubtaskResult(agent=agent, query="q", content="c", **kw)


class TestResultPredicates:
    def test_a_plain_done_step_is_done_and_not_failed(self):
        r = res()
        assert _result_done(r) and not _result_failed(r)

    def test_an_unfinished_outcome_or_an_error_is_failed_and_not_done(self):
        for r in (res(outcome="stuck"), res(error="boom"), res(outcome="out_of_time")):
            assert _result_failed(r) and not _result_done(r)

    def test_spawn_bookkeeping_is_excluded_from_both(self):
        for r in (res("__spawn__"), res("__spawn__", error="spawn refused"), res("__spawn__", outcome="stuck")):
            assert not _result_failed(r) and not _result_done(r)

    def test_a_superseded_step_is_excluded_from_both(self):
        for r in (res(outcome="stuck", superseded=True), res(superseded=True)):
            assert not _result_failed(r) and not _result_done(r)

    def test_a_condition_skipped_step_is_neither_failed_nor_done(self):
        r = res(skipped=True)                      # skip_when matched: no error
        assert not _result_failed(r) and not _result_done(r)

    def test_a_dependency_skipped_step_is_failed(self):
        r = res(skipped=True, error="skipped: dependency s1 failed")
        assert _result_failed(r) and not _result_done(r)


class TestSupervisorOutcome:
    def test_nothing_unresolved_is_done_even_with_a_budget_reason(self):
        assert _supervisor_outcome([res()]) == "done"
        assert _supervisor_outcome([res()], "out_of_time") == "done"
        assert _supervisor_outcome([]) == "done"

    def test_spawn_steps_and_superseded_failures_do_not_make_it_unresolved(self):
        results = [res("__spawn__", error="x"), res(outcome="stuck", superseded=True), res()]
        assert _supervisor_outcome(results, "out_of_budget") == "done"

    def test_something_unresolved_with_a_budget_reason_reports_the_reason(self):
        results = [res(), res(outcome="stuck")]
        assert _supervisor_outcome(results, "out_of_time") == "out_of_time"
        assert _supervisor_outcome([res(outcome="stuck")], "out_of_budget") == "out_of_budget"

    def test_partial_when_some_step_is_done_and_stuck_when_none_is(self):
        assert _supervisor_outcome([res(), res(outcome="stuck")]) == "partial"
        assert _supervisor_outcome([res(outcome="stuck"), res(error="boom")]) == "stuck"

    def test_a_condition_skipped_step_alone_does_not_count_as_done(self):
        assert _supervisor_outcome([res(skipped=True), res(outcome="stuck")]) == "stuck"
