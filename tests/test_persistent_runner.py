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
