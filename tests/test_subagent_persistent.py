"""Sub-agents under persistent mode: recovery by spawning, inherited persistence, shared deadline."""

import asyncio

from agentx_dev import (
    AgentRunner, AgentType, AsyncAgentRunner, AsyncSupervisor, InMemoryCache, Persistence, Supervisor,
)
from agentx_dev.Supervisor import SpawnConfig
from agentx_dev.SubAgents import AgentSpec, SpawnPolicy
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import (
    PLANNER_MARK, SPAWNED_MARK, SYNTH_MARK, ScriptedRunner, plan_json, router, step,
)

P = Persistence(max_minutes=5)


class AsyncScriptModel(MockModel):
    """A model whose async calls run an async script, so a test can order concurrent turns."""

    def __init__(self, afn):
        super().__init__(script=lambda m: "")
        self._afn = afn

    async def async_initialize(self, messages):
        self.calls.append(list(messages))
        return await self._afn(messages)


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
        assert result.subtasks[0].superseded is False         # nothing replaced s1
        assert result.outcome == "stuck"                      # so s1 stays unresolved

    def test_an_explicit_ceiling_can_allow_more(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step(tools=("code",)))],
                       sub=lambda m: make_final("fixed"))
        worker = ScriptedRunner(("gave up", "stuck"))
        cfg = SpawnConfig(enabled=True, capabilities={"web", "code"}, allowed_paths=["./workspace"])
        result = sync_sup(model, {"worker": ("w", worker)}, spawn_config=cfg).run("task")
        assert result.outcome == "done" and result.subtasks[-1].agent == "fixer"
        assert result.spawned[0]["tools"] == ["code"] and result.spawned[0]["dropped"] == []


class TestInheritance:
    def test_spawned_agents_carry_persistence(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = sync_sup(model, {})
        s.run("task")
        runner = s._spawn_policy().run.built["a"].runner
        assert runner.persistence is P and runner.max_iterations == P.max_turns

    def test_persistence_on_a_spawned_agent_suspends_its_tool_cache_and_clearing_it_restores_it(self):
        # Built without persistence, so the cache we configure is the only thing in play; the same
        # runner then goes through the persistence setter that SpawnPolicy.build() applies.
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), MockModel(), verbose=False)
        runner = policy.build(AgentSpec(name="a", instructions="Be useful.", tools=("web",))).runner
        assert runner.persistence is None
        cache = InMemoryCache()
        runner.registry.configure_cache(cache, 60)
        runner.persistence = P
        assert runner.registry.cache is None                  # a cached retry is not a retry
        runner.persistence = None
        assert runner.registry.cache is cache and runner.registry._cache_ttl == 60

    def test_async_spawned_agents_carry_persistence_too(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = async_sup(model, {})
        asyncio.run(s.run("task"))
        runner = s._spawn_policy().run.built["a"].runner
        assert isinstance(runner, AsyncAgentRunner) and runner.persistence is P
        assert runner.max_iterations == P.max_turns


class TestSharedDeadline:
    def test_a_plan_spawned_agent_gets_the_same_budget_as_a_registered_specialist(self, monkeypatch):
        budgets = []
        original = AgentRunner.Initialize

        def spy(self, user_input, *a, **kw):
            budgets.append((self, kw.get("_budget")))
            return original(self, user_input, *a, **kw)

        monkeypatch.setattr(AgentRunner, "Initialize", spy)
        model = router(plans=[plan_json(
            {"id": "s1", "query": "q", "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}},
            step("s2", "worker"))], sub=lambda m: make_final("x"))
        worker = ScriptedRunner()
        s = sync_sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.outcome == "done" and [x["origin"] for x in result.spawned] == ["plan"]
        spawned_runner = s._spawn_policy().run.built["a"].runner
        spawned_budget = dict((id(r), b) for r, b in budgets)[id(spawned_runner)]
        worker_budget = worker.calls[0][1]
        assert spawned_budget is not None and worker_budget is not None
        assert spawned_budget.deadline == worker_budget.deadline
        assert spawned_budget is worker_budget                # one RunBudget for the whole run

    def test_a_recovery_spawned_agent_gets_the_same_budget_as_the_stuck_specialist(self, monkeypatch):
        budgets = []
        original = AgentRunner.Initialize

        def spy(self, user_input, *a, **kw):
            budgets.append((self, kw.get("_budget")))
            return original(self, user_input, *a, **kw)

        monkeypatch.setattr(AgentRunner, "Initialize", spy)
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step())],
                       sub=lambda m: make_final("fixed"), synth="All done.")
        worker = ScriptedRunner(("gave up", "stuck"))
        s = sync_sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.outcome == "done" and result.subtasks[-1].agent == "fixer"
        fixer_runner = s._spawn_policy().run.built["fixer"].runner
        fixer_budget = dict((id(r), b) for r, b in budgets)[id(fixer_runner)]
        worker_budget = worker.calls[0][1]
        assert fixer_budget is not None and fixer_budget is worker_budget

    def test_the_same_in_async(self, monkeypatch):
        budgets = []
        original = AsyncAgentRunner.Initialize

        async def spy(self, user_input, *a, **kw):
            budgets.append((self, kw.get("_budget")))
            return await original(self, user_input, *a, **kw)

        monkeypatch.setattr(AsyncAgentRunner, "Initialize", spy)
        model = router(plans=[plan_json(
            {"id": "s1", "query": "q", "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}},
            step("s2", "worker"))], sub=lambda m: make_final("x"))
        worker = AsyncStuck()
        s = async_sup(model, {"worker": ("w", worker)})
        result = asyncio.run(s.run("task"))
        assert result.outcome == "done" and [x["origin"] for x in result.spawned] == ["plan"]
        spawned_runner = s._spawn_policy().run.built["a"].runner
        spawned_budget = dict((id(r), b) for r, b in budgets)[id(spawned_runner)]
        worker_budget = worker.calls[0][1]
        assert spawned_budget is not None and spawned_budget is worker_budget

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
        assert s._spawn_policy().budget is worker_budget      # the run's budget is on the policy too

    def test_a_delegation_keeps_the_deadline_when_a_parallel_step_on_the_same_specialist_ends_first(
            self, monkeypatch):
        # Two parallel steps run on ONE async specialist. Step A finishes (which clears the
        # runner's _active_budget) while step B is still running; B then delegates. The
        # sub-agent must still get the supervisor's RunBudget, not start a fresh one.
        budgets = []
        original = AsyncAgentRunner.Initialize
        b_started, a_finished = asyncio.Event(), asyncio.Event()
        seen_cleared = []

        async def spy(self, user_input, *a, **kw):
            budgets.append((self, user_input, kw.get("_budget")))
            try:
                return await original(self, user_input, *a, **kw)
            finally:
                if user_input == "job A":
                    a_finished.set()

        monkeypatch.setattr(AsyncAgentRunner, "Initialize", spy)
        b_turns = []

        async def script(messages):
            first, text = str(messages[0]["content"]), str(messages)
            if PLANNER_MARK in first:
                return plan_json(step("s1", "worker", "job A"), step("s2", "worker", "job B"))
            if SYNTH_MARK in first:
                return "Final."
            if SPAWNED_MARK in first:
                return make_final("X is 42")
            if "job B" in text:
                b_turns.append(1)
                if len(b_turns) == 1:
                    b_started.set()
                    await a_finished.wait()
                    seen_cleared.append(worker._active_budget is None)
                    return make_react_response("delegate", {"task": "find X", "tools": ["web"]})
                return make_final("B done")
            await b_started.wait()
            return make_final("A done")

        model = AsyncScriptModel(script)
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        s = async_sup(model, {"worker": ("w", worker)})
        result = asyncio.run(s.run("task"))
        assert result.outcome == "done" and [x["origin"] for x in result.spawned] == ["delegate"]
        assert seen_cleared == [True]                         # A really had cleared the runner's budget
        worker_budgets = [b for r, q, b in budgets if r is worker]
        assert len(worker_budgets) == 2 and worker_budgets[0] is worker_budgets[1] is not None
        sub_runner = s._spawn_policy().run.built["delegate_1"].runner
        [sub_budget] = [b for r, q, b in budgets if r is sub_runner]
        assert sub_budget is not None and sub_budget.deadline == worker_budgets[0].deadline
        assert sub_budget is worker_budgets[0]

    def test_a_stuck_delegation_does_not_end_the_specialists_run_under_persistence(self):
        worker_calls = []

        def worker_script(messages):
            worker_calls.append(str(messages))
            if len(worker_calls) == 1:
                return make_react_response("delegate", {"task": "t", "tools": ["web"]})
            return make_final("worker carried on")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: (_ for _ in ()).throw(RuntimeError("provider down for good")))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        assert result.subtasks[0].content == "worker carried on" and result.outcome == "done"
        assert result.spawned[0]["origin"] == "delegate" and result.spawned[0]["outcome"] == "error"
        assert len(worker_calls) == 2
        assert "[delegate failed: error] provider down for good" in worker_calls[1]
