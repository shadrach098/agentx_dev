"""Sub-agents under persistent mode: recovery by spawning, inherited persistence, shared deadline."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, AsyncSupervisor, Persistence, Supervisor
from agentx_dev.Supervisor import SpawnConfig
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step

P = Persistence(max_minutes=5)


def fixer_step(tools=("web",)):
    return {"id": "fix", "query": "another way", "replaces": ["s1"],
            "new_agent": {"name": "fixer", "instructions": "Do it differently. Return the data.",
                          "tools": list(tools)}}


class AsyncStuck(ScriptedRunner):
    async def Initialize(self, query, _budget=None):
        return ScriptedRunner.Initialize(self, query, _budget)


def sync_sup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents=agents, verbose=False, persistence=P, **kw)


def async_sup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents=agents, verbose=False, persistence=P, **kw)


class TestRecoveryBySpawning:
    def test_a_recovery_round_replaces_a_stuck_step_with_a_new_agent(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step())],
                       sub=lambda m: make_final("fixed"), synth="All done.")
        worker = ScriptedRunner(("gave up", "stuck"))
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        assert result.outcome == "done" and result.content == "All done."
        assert [(r.step_id, r.superseded, r.outcome) for r in result.subtasks] == [
            ("s1", True, "stuck"), ("fix", False, "done")]
        assert result.subtasks[1].agent == "fixer" and result.subtasks[1].content == "fixed"
        assert [(x["name"], x["origin"]) for x in result.spawned] == [("fixer", "plan")]
        recovery_prompt = model.planner_prompts()[1]
        assert "RECOVERY ROUND 2" in recovery_prompt and "CREATING NEW SPECIALISTS" in recovery_prompt

    def test_the_same_recovery_in_async(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step())],
                       sub=lambda m: make_final("fixed"), synth="All done.")
        worker = AsyncStuck(("gave up", "stuck"))
        result = asyncio.run(async_sup(model, {"worker": ("w", worker)}).run("task"))
        assert result.outcome == "done"
        assert [(r.step_id, r.superseded) for r in result.subtasks] == [("s1", True), ("fix", False)]
        assert result.subtasks[1].agent == "fixer" and [x["name"] for x in result.spawned] == ["fixer"]

    def test_the_default_ceiling_clips_code_execution(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step(tools=("code",)))],
                       sub=lambda m: make_final("fixed"))
        worker = ScriptedRunner(("gave up", "stuck"))
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        fix = [r for r in result.subtasks if r.step_id == "fix"][0]
        assert fix.error == "spawn refused: no usable tools"
        assert result.outcome != "done"                       # s1 stays unresolved, nothing replaced it

    def test_an_explicit_ceiling_can_allow_more(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step(tools=("code",)))],
                       sub=lambda m: make_final("fixed"))
        worker = ScriptedRunner(("gave up", "stuck"))
        cfg = SpawnConfig(enabled=True, capabilities={"web", "code"}, allowed_paths=["./workspace"])
        result = sync_sup(model, {"worker": ("w", worker)}, spawn_config=cfg).run("task")
        assert result.outcome == "done" and result.subtasks[-1].agent == "fixer"


class TestInheritance:
    def test_spawned_agents_carry_persistence_and_run_without_the_tool_cache(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = sync_sup(model, {})
        s.run("task")
        runner = s._spawn_policy().run.built["a"].runner
        assert runner.persistence is P and runner.max_iterations == P.max_turns
        assert getattr(runner.registry, "cache", None) is None

    def test_async_spawned_agents_carry_persistence_too(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = async_sup(model, {})
        asyncio.run(s.run("task"))
        runner = s._spawn_policy().run.built["a"].runner
        assert isinstance(runner, AsyncAgentRunner) and runner.persistence is P


class TestSharedDeadline:
    def test_a_delegated_sub_agent_gets_the_deadline_the_supervisor_gave_its_specialist(self, monkeypatch):
        budgets = []
        original = AgentRunner.Initialize

        def spy(self, user_input, *a, **kw):
            budgets.append((self, kw.get("_budget")))
            return original(self, user_input, *a, **kw)

        monkeypatch.setattr(AgentRunner, "Initialize", spy)
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "find X", "tools": ["web"]})
            return make_final("worker done")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: make_final("X is 42"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        s = sync_sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.outcome == "done" and result.spawned[0]["origin"] == "delegate"
        by_runner = {id(r): b for r, b in budgets}
        worker_budget = by_runner[id(worker)]
        sub_runner = s._spawn_policy().run.built["delegate_1"].runner
        sub_budget = by_runner[id(sub_runner)]
        assert worker_budget is not None and sub_budget is not None
        assert sub_budget.deadline == worker_budget.deadline

    def test_a_stuck_delegation_does_not_end_the_specialists_run_under_persistence(self):
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "t", "tools": ["web"]})
            return make_final("worker carried on")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: (_ for _ in ()).throw(RuntimeError("provider down for good")))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        assert result.subtasks[0].content == "worker carried on" and result.outcome == "done"
