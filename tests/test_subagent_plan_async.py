"""AsyncSupervisor: new_agent steps, reuse, the per-run registry, delegation, parallel spawned steps."""

import asyncio

import pytest

from agentx_dev import AgentType, AsyncAgentRunner, AsyncSupervisor, Persistence
from agentx_dev.Supervisor import SpawnConfig
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import (
    SPAWNED_MARK, ScriptedRunner, new_agent_step, plan_json, router, step,
)

WEB = SpawnConfig(enabled=True, capabilities={"web"})


class AsyncScripted(ScriptedRunner):
    async def Initialize(self, query, _budget=None):
        return ScriptedRunner.Initialize(self, query, _budget)


def asup(model, agents=None, cfg=WEB, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents=agents or {}, spawn_config=cfg, verbose=False, **kw)


def run(s, task="task"):
    return asyncio.run(s.run(task))


async def _collect(s, task="task", stop=None):
    out = []
    gen = s.astream(task)
    try:
        async for ev in gen:
            out.append(ev)
            if stop and stop(ev):
                break
    finally:
        await gen.aclose()
    return out


def events_of(s, task="task", stop=None):
    return asyncio.run(_collect(s, task, stop))


class Barrier:
    """Both spawned steps must be in flight at the same moment, or the wait times out."""

    def __init__(self, n):
        self.n, self.seen, self.evt = n, 0, asyncio.Event()

    async def hit(self):
        self.seen += 1
        if self.seen >= self.n:
            self.evt.set()
        await asyncio.wait_for(self.evt.wait(), 2)


class TestAsyncNewAgentSteps:
    def test_spawns_an_async_runner_runs_the_step_and_discards_it(self):
        model = router(plans=[plan_json(new_agent_step(
            "s1", "analyst", "You compare prices.", ["web"], query="compare"))],
            sub=lambda m: make_final("TABLE"))
        s = asup(model)
        result = run(s)
        assert result.subtasks[0].agent == "analyst" and result.subtasks[0].content == "TABLE"
        assert result.outcome == "done" and s.agents == {}
        assert result.spawned == [{"name": "analyst", "origin": "plan", "tools": ["web"],
                                   "dropped": [], "outcome": "done", "chars": 5}]

    def test_the_planned_agent_is_an_async_runner_and_gets_the_instructions(self):
        seen = []
        model = router(plans=[plan_json(new_agent_step("s1", "a", "You are the analyst.", ["web", "code"]))],
                       sub=lambda m: seen.append(str(m[0]["content"])) or make_final("x"))
        s = asup(model)
        events_of(s)
        assert "You are the analyst." in seen[0] and "- web_search :" in seen[0]
        assert "- run_python :" not in seen[0]
        assert isinstance(s._spawn_policy().run.built["a"].runner, AsyncAgentRunner)

    def test_the_prompt_teaches_new_agent_only_when_enabled(self):
        on = router(plans=[plan_json(step("s1", "w"))])
        run(asup(on, {"w": ("w", AsyncScripted())}))
        assert "CREATING NEW SPECIALISTS" in on.planner_prompts()[0]
        off = router(plans=[plan_json(step("s1", "w"))])
        run(AsyncSupervisor(model=off, agents={"w": ("w", AsyncScripted())}, verbose=False))
        assert "new_agent" not in off.planner_prompts()[0]

    def test_reuse_by_name_collision_suffix_and_registered_names(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "one", ["web"]),
            new_agent_step("s2", "a", "one", ["web"]),
            new_agent_step("s3", "a", "two", ["web"]),
            new_agent_step("s4", "worker", "ignored", ["web"]))], sub=lambda m: make_final("x"))
        worker = AsyncScripted(("registered answer", "done"))
        s = asup(model, {"worker": ("w", worker)})
        result = run(s)
        assert sorted(r.agent for r in result.subtasks) == ["a", "a", "a_2", "worker"]
        assert [x["name"] for x in result.spawned] == ["a", "a_2"]
        reused = [e["reused"] for e in events_of(asup(
            router(plans=[plan_json(new_agent_step("s1", "a", "one", ["web"]),
                                    new_agent_step("s2", "a", "one", ["web"]))], sub=lambda m: make_final("x"))))
            if e["type"] == "spawn"]
        assert reused == [None, "spawned"]

    def test_refusals_fail_the_step_with_the_reason(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])
        assert run(asup(model)).subtasks[0].error == "spawn refused: no usable tools"
        off = router(plans=[plan_json(new_agent_step("s1", "a", "x"))])
        r = run(AsyncSupervisor(model=off, agents={}, verbose=False, max_subtask_retries=0))
        assert r.subtasks[0].error == "spawn refused: spawning is disabled"
        lim = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]),
                                      new_agent_step("s2", "b", "y", ["web"]))], sub=lambda m: make_final("x"))
        r = run(asup(lim, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1), sequential=True))
        assert r.subtasks[0].error is None and r.subtasks[1].error == "spawn refused: spawn limit reached"

    def test_a_malformed_definition_triggers_the_repair_retry(self):
        bad = plan_json({"id": "s1", "query": "q", "new_agent": {"name": "a"}})
        good = plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))
        model = router(plans=[bad, good], sub=lambda m: make_final("x"))
        result = run(asup(model))
        assert len(model.planner_prompts()) == 2 and "new_agent invalid" in model.planner_prompts()[1]
        assert result.subtasks[0].agent == "a"

    def test_a_new_agent_named_spawn_fails_the_run_instead_of_vanishing(self):
        bad = plan_json({"id": "s1", "query": "q",
                         "new_agent": {"name": "__spawn__", "instructions": "Be useful.", "tools": ["web"]}})
        model = router(plans=[bad, bad], sub=lambda m: make_final("x"))
        result = run(asup(model))
        assert len(model.planner_prompts()) == 2 and "name is reserved" in model.planner_prompts()[1]
        assert result.content == "Supervisor failed to produce a valid plan."
        assert result.outcome == "stuck" and result.subtasks == [] and result.spawned == []

    def test_the_stream_carries_the_spawn_event(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))],
                       sub=lambda m: make_final("x"))
        evs = [e for e in events_of(asup(model)) if e["type"] == "spawn"]
        assert evs == [{"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"], "dropped": [],
                        "reused": None, "refused": None, "capabilities": ["web"], "rerouted_from": None}]

    def test_a_skipped_step_does_not_spend_a_spawn(self):
        model = router(plans=[plan_json(step("s1", "bad"),
                                        new_agent_step("s2", "a", "x", ["web"], deps=["s1"]))],
                       sub=lambda m: make_final("x"))
        result = run(asup(model, {"bad": ("b", AsyncScripted(("nope", "stuck")))}))
        assert result.subtasks[1].skipped and result.spawned == []


class TestAsyncLegacySpawn:
    def test_the_3_5_spawn_step_works_in_async_too(self, tmp_path):
        cfg = SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)])
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "reads files",
             "capabilities": ["files"]},
            step("s1", "scout", "look"))], sub=lambda m: make_final("scouted"))
        s = asup(model, cfg=cfg, sequential=True)
        result = run(s)
        assert result.subtasks[0].agent == "__spawn__" and result.subtasks[0].content.startswith("registered new specialist 'scout'")
        assert result.subtasks[1].agent == "scout" and result.subtasks[1].content == "scouted"
        assert "scout" not in s.agents

    def test_a_refused_legacy_spawn_is_reported(self):
        cfg = SpawnConfig(enabled=True, approver=lambda r: False)
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]})])
        assert run(asup(model, cfg=cfg)).subtasks[0].error == "spawn refused (see log for reason)"


class TestAsyncPerRunRegistry:
    def test_closing_the_stream_early_restores_everything(self):
        worker = AsyncAgentRunner(model=router(), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "worker", deps=["s1"]))],
                       sub=lambda m: make_final("x"), agent=lambda m: make_final("w"))
        worker.model = model
        s = asup(model, {"worker": ("w", worker)})
        before = dict(s.agents)
        events_of(s, stop=lambda ev: ev["type"] == "spawn")
        assert s.agents == before and "a" not in s.agents
        assert "delegate" not in worker.registry.names

    def test_consecutive_runs_do_not_share_spawns(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]))] * 2,
                       sub=lambda m: make_final("x"))
        s = asup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        assert run(s, "one").spawned[0]["name"] == "a"
        second = run(s, "two")
        assert second.subtasks[0].error is None and len(second.spawned) == 1


class TestParallelSpawnedSteps:
    def test_two_independent_new_agent_steps_run_at_the_same_time(self):
        barrier = Barrier(2)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "one", ["web"]),
                                        new_agent_step("s2", "b", "two", ["web"]))],
                       sub=lambda m: make_final("x"))
        original = model.async_initialize

        async def gated(messages):
            if SPAWNED_MARK in str(messages[0]["content"]):
                await barrier.hit()
            return await original(messages)

        model.async_initialize = gated
        result = run(asup(model))
        assert barrier.seen == 2 and result.outcome == "done"
        assert sorted(r.agent for r in result.subtasks) == ["a", "b"]


class TestAsyncSpecialistsDelegate:
    def test_specialists_have_delegate_during_the_run_and_lose_it_after(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        run(asup(model, {"worker": ("w", worker)}))
        assert seen == [True] and "delegate" not in worker.registry.names

    def test_a_specialist_delegates_and_the_record_and_events_show_it(self):
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "find X", "instructions": "You find X.",
                                                        "tools": ["web"]})
            return make_final("worker done")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: make_final("X is 42"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        evs = events_of(asup(model, {"worker": ("w", worker)}))
        done = [e for e in evs if e["type"] == "completion"][0]["result"]
        assert done.subtasks[0].content == "worker done"
        assert done.spawned == [{"name": "delegate_1", "origin": "delegate", "tools": ["web"],
                                 "dropped": [], "outcome": "done", "chars": 7}]
        kinds = [e["type"] for e in evs]
        assert kinds.index("spawn") < kinds.index("delegate_result") < kinds.index("subtask_result")

    def test_no_spawn_config_means_no_delegate(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = run(AsyncSupervisor(model=model, agents={"worker": ("w", worker)}, verbose=False))
        assert seen == [False] and result.spawned == []

    def test_a_legacy_spawn_config_gives_specialists_no_delegate(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append(("delegate" in worker.registry.names,
                                                    "delegate" in str(m[0]["content"]))) or make_final("ok"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        legacy = SpawnConfig(enabled=True, auto_spawn=True)
        result = run(asup(model, {"worker": ("w", worker)}, cfg=legacy))
        assert seen == [(False, False)] and result.spawned == []
        assert "CREATING NEW SPECIALISTS" in model.planner_prompts()[0]     # new_agent still taught


class TestAsyncPersistentDefault:
    def test_persistent_default_and_explicit_override(self):
        s = AsyncSupervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5))
        cfg = s.spawn_config
        assert cfg.enabled and cfg.capabilities == {"web", "files_read"} and cfg.effective_max_spawns == 6
        off = AsyncSupervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5),
                              spawn_config=SpawnConfig(enabled=False))
        assert not off.spawn_config.enabled
        assert not AsyncSupervisor(model=router(), agents={}, verbose=False).spawn_config.enabled


class TestAsyncUnbuiltAgentStep:
    NOT_IN_REGISTRY = "specialist 'scout' not in registry (spawn may have been refused)"

    def test_a_refused_legacy_spawn_then_a_step_on_it_fails_without_crashing(self):
        cfg = SpawnConfig(enabled=True, approver=lambda r: False)
        for seq in (False, True):
            model = router(plans=[plan_json(
                {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]},
                step("s1", "scout", "look"))])
            result = run(asup(model, cfg=cfg, sequential=seq))
            assert result.subtasks[0].error == "spawn refused (see log for reason)"
            assert result.subtasks[1].agent == "scout" and result.subtasks[1].error == self.NOT_IN_REGISTRY

    def test_a_refused_new_agent_then_a_step_on_its_name_fails(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "x", ["code"]),
            {"id": "s2", "agent": "a", "query": "q"})])
        result = run(asup(model))
        by_agent = {r.step_id: r for r in result.subtasks}
        assert by_agent["s1"].error == "spawn refused: no usable tools"
        assert by_agent["s2"].error == "specialist 'a' not in registry (spawn may have been refused)"

    def test_spawning_disabled_a_spawn_step_and_its_user_do_not_crash(self):
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]},
            step("s1", "scout", "look"))])
        result = run(AsyncSupervisor(model=model, agents={}, verbose=False, max_subtask_retries=0))
        assert result.subtasks[-1].agent == "scout" and result.subtasks[-1].error == self.NOT_IN_REGISTRY
