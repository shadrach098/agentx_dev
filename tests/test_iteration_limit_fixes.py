"""Unknown-tool message, helper step budget, iteration-limit retry, SpawnConfig.max_iterations."""

import asyncio

from agentx_dev import StandardTool, ToolRegistry
from agentx_dev.Agents.Agent import ToolError
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from agentx_dev.Supervisor import AsyncSupervisor, Supervisor
from tests.conftest import MockModel
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step
from agentx_dev.Supervisor import (
    _augment_query_with_iteration_limit,
    _augment_query_with_unmet_criteria,
)


def _registry():
    return ToolRegistry([StandardTool(func=lambda q: "x", name="web_search", description="d")])


class TestUnknownToolMessage:

    def test_sync_lists_valid_tools_and_final_answer(self):
        result = _registry().dispatch("nope", "hi")
        assert isinstance(result, ToolError)
        assert "web_search" in result
        assert "Final_Answer" in result

    def test_format_name_is_called_out(self):
        result = _registry().dispatch("React_", "hi")
        assert "not a tool" in result
        assert "web_search" in result

    def test_async_lists_valid_tools_and_format_hint(self):
        reg = _registry()
        plain = asyncio.run(reg.adispatch("nope", "hi"))
        fmt = asyncio.run(reg.adispatch("React_", "hi"))
        assert "web_search" in plain and "Final_Answer" in plain
        assert "not a tool" in fmt

    def test_no_tools_registered(self):
        result = ToolRegistry([]).dispatch("nope", "hi")
        assert isinstance(result, ToolError)
        assert "no tools" in result.lower()


class TestStepBudgetLine:

    def _runner(self, **cfg):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}, **cfg), router())
        return policy._make_runner(AgentSpec(name="scout", instructions="find things", tools=("web",)),
                                   ["web"], [], False)

    def test_default_budget_in_the_addendum(self):
        runner = self._runner()
        assert "at most 15 steps" in runner.system_addendum
        assert runner.max_iterations == 15

    def test_configured_budget(self):
        runner = self._runner(max_iterations=30)
        assert "at most 30 steps" in runner.system_addendum
        assert runner.max_iterations == 30

    def test_zero_is_coerced_to_one(self):
        runner = self._runner(max_iterations=0)
        assert runner.max_iterations == 1
        assert "at most 1 step." in runner.system_addendum


class TestRetryNote:

    def test_iteration_limit_note_has_recap_and_advice(self):
        note = _augment_query_with_iteration_limit("find X", "Steps taken: searched A, B, C", 1)
        assert "ran out of steps" in note
        assert "Steps taken: searched A, B, C" in note
        assert "find X" in note
        assert "change your METHOD" not in note

    def test_recap_is_trimmed(self):
        note = _augment_query_with_iteration_limit("q", "z" * 5000, 1)
        assert note.count("z") <= 1500

    def test_generic_note_unchanged(self):
        assert "change your METHOD" in _augment_query_with_unmet_criteria("q", "stuck", 1)


class _AsyncScripted(ScriptedRunner):
    async def Initialize(self, query, _budget=None):          # type: ignore[override]
        return ScriptedRunner.Initialize(self, query, _budget)


def _one_step():
    return plan_json(step("s1", "worker", "do the thing"))


class TestRetryAfterRunningOutOfSteps:

    def _sup(self, cls, worker):
        return cls(model=MockModel(script=[_one_step(), "Final."]), agents={"worker": ("worker", worker)},
                   verbose=False, max_subtask_retries=1)

    def test_sync_retry_tells_the_helper_it_ran_out_and_shows_the_recap(self):
        worker = ScriptedRunner(("Steps taken: searched A, then B", "iteration_limit"), ("fine", "done"))
        result = self._sup(Supervisor, worker).run("task")
        second = worker.calls[1][0]
        assert "ran out of steps" in second and "searched A, then B" in second
        assert "change your METHOD" not in second
        assert result.subtasks[0].content == "fine"

    def test_async_retry_tells_the_helper_it_ran_out_and_shows_the_recap(self):
        worker = _AsyncScripted(("Steps taken: searched A, then B", "iteration_limit"), ("fine", "done"))
        result = asyncio.run(self._sup(AsyncSupervisor, worker).run("task"))
        second = worker.calls[1][0]
        assert "ran out of steps" in second and "searched A, then B" in second
        assert "change your METHOD" not in second
        assert result.subtasks[0].content == "fine"

    def test_other_failures_keep_the_generic_note(self):
        worker = ScriptedRunner(("gave up", "stuck"), ("fine", "done"))
        self._sup(Supervisor, worker).run("task")
        assert "change your METHOD" in worker.calls[1][0]
        assert "ran out of steps" not in worker.calls[1][0]
