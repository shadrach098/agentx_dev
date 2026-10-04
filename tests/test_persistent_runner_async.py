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
