"""Supervisor: new_agent plan steps, reuse and collisions, the per-run registry, delegation."""

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, Supervisor
from agentx_dev.Supervisor import SpawnConfig, _sanitize_plan
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import (
    ScriptedRunner, new_agent_step, plan_json, router, step,
)

WEB = SpawnConfig(enabled=True, capabilities={"web"})


def sup(model, agents=None, cfg=WEB, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents=agents or {}, spawn_config=cfg, verbose=False, **kw)


def events_of(supervisor, task="task"):
    return list(supervisor.stream(task))


class TestSanitizer:
    def test_a_valid_new_agent_is_normalized_and_names_its_step_agent(self):
        sane, repairs = _sanitize_plan([new_agent_step("s1", " a ", " Be useful. ", ["web", "web"])])
        assert repairs == []
        assert sane[0]["agent"] == "a"
        assert sane[0]["new_agent"] == {"name": "a", "instructions": "Be useful.", "tools": ["web"]}

    def test_sanitizing_twice_is_stable(self):
        once, _ = _sanitize_plan([new_agent_step("s1", "a", "Be useful.", ["web"])])
        twice, repairs = _sanitize_plan(once)
        assert twice == once and repairs == []

    @pytest.mark.parametrize("bad", [
        {"name": "has space", "instructions": "i"},
        {"name": "a"},
        {"name": "a", "instructions": "i", "tools": "web"},
        "not an object",
    ])
    def test_a_malformed_new_agent_drops_the_step_and_reports_it(self, bad):
        plan = [{"id": "s1", "query": "q", "new_agent": bad}, step("s2", "w", deps=["s1"])]
        sane, repairs = _sanitize_plan(plan)
        assert [s["id"] for s in sane] == ["s2"]
        assert sane[0]["depends_on"] == []                      # the dependency on the dropped step is cleaned
        assert any("new_agent invalid" in r for r in repairs) and any("unknown dependency" in r for r in repairs)

    @pytest.mark.parametrize("name", ["__spawn__", "__x", "delegate_1"])
    def test_a_reserved_new_agent_name_drops_the_step_with_a_repair(self, name):
        plan = [{"id": "s1", "query": "q", "new_agent": {"name": name, "instructions": "i"}},
                step("s2", "w")]
        sane, repairs = _sanitize_plan(plan)
        assert [s["id"] for s in sane] == ["s2"]
        assert repairs == ["step 's1': new_agent invalid (name is reserved) -- dropped"]

    def test_agent_and_a_different_new_agent_name_together_keep_the_step_but_drop_the_definition(self):
        plan = [{"id": "s1", "agent": "worker", "query": "q",
                 "new_agent": {"name": "other", "instructions": "i"}}]
        sane, repairs = _sanitize_plan(plan)
        assert sane[0]["agent"] == "worker" and "new_agent" not in sane[0]
        assert any("both" in r for r in repairs)


class TestPlannerPrompt:
    def test_enabled_spawning_teaches_new_agent_with_the_menu(self):
        model = router(plans=[plan_json(step("s1", "w"))])
        sup(model, {"w": ("w", ScriptedRunner())}).run("task")
        prompt = model.planner_prompts()[0]
        assert "CREATING NEW SPECIALISTS" in prompt and "new_agent" in prompt
        assert "web:" in prompt and "files:" not in prompt and "at most 6" in prompt

    def test_disabled_spawning_leaves_the_prompt_alone(self):
        model = router(plans=[plan_json(step("s1", "w"))])
        Supervisor(model=model, agents={"w": ("w", ScriptedRunner())}, verbose=False).run("task")
        assert "new_agent" not in model.planner_prompts()[0]


class TestNewAgentSteps:
    def test_spawns_runs_the_step_and_discards_the_agent_afterwards(self):
        model = router(plans=[plan_json(new_agent_step(
            "s1", "pricing_analyst", "You compare prices. Return a table.", ["web"], query="compare"))],
            sub=lambda m: make_final("TABLE"))
        s = sup(model)
        result = s.run("task")
        assert result.subtasks[0].agent == "pricing_analyst" and result.subtasks[0].content == "TABLE"
        assert result.outcome == "done" and result.content == "Final."
        assert s.agents == {}                                   # nothing leaked onto the supervisor
        assert result.spawned == [{"name": "pricing_analyst", "origin": "plan", "tools": ["web"],
                                   "dropped": [], "outcome": "done", "chars": 5}]

    def test_the_stream_carries_the_spawn_event(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))],
                       sub=lambda m: make_final("x"))
        evs = [e for e in events_of(sup(model)) if e["type"] == "spawn"]
        assert evs == [{"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"], "dropped": [],
                        "reused": None, "refused": None, "capabilities": ["web"], "rerouted_from": None}]

    def test_the_sub_agent_gets_the_planners_instructions_and_only_the_granted_tools(self):
        seen = []
        model = router(plans=[plan_json(new_agent_step("s1", "a", "You are the pricing analyst.", ["web", "code"]))],
                       sub=lambda m: seen.append(str(m[0]["content"])) or make_final("x"))
        sup(model).run("task")
        system = seen[0]
        assert "You are the pricing analyst." in system and "- web_search :" in system
        assert "- run_python :" not in system                   # the tool list, not the template's prose

    def test_a_later_step_reuses_the_agent_by_name_for_one_spawn(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "Be useful.", ["web"]),
            step("s2", "a", "again", deps=["s1"]))], sub=lambda m: make_final("x"))
        s = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        result = s.run("task")
        assert [r.agent for r in result.subtasks] == ["a", "a"] and len(result.spawned) == 1

    def test_an_identical_redefinition_reuses_and_a_different_one_is_suffixed(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "one", ["web"]),
            new_agent_step("s2", "a", "one", ["web"]),
            new_agent_step("s3", "a", "two", ["web"]))], sub=lambda m: make_final("x"))
        result = sup(model).run("task")
        assert [r.agent for r in result.subtasks] == ["a", "a", "a_2"]
        assert [s["name"] for s in result.spawned] == ["a", "a_2"]

    def test_a_name_registered_by_the_developer_dispatches_that_specialist(self):
        worker = ScriptedRunner(("from the registered one", "done"))
        model = router(plans=[plan_json(new_agent_step("s1", "worker", "ignored", ["web"]))])
        s = sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.subtasks[0].content == "from the registered one" and result.spawned == []
        evs = [e for e in events_of(sup(router(plans=[plan_json(new_agent_step("s1", "worker", "ignored"))]),
                                        {"worker": ("w", ScriptedRunner())})) if e["type"] == "spawn"]
        assert evs[0]["reused"] == "registered"

    def test_a_refused_spawn_fails_the_step_with_the_reason(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])
        result = sup(model).run("task")
        assert result.subtasks[0].error == "spawn refused: no usable tools"
        assert result.outcome != "done"
        evs = [e for e in events_of(sup(router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])))
               if e["type"] == "spawn"]
        assert evs[0]["refused"] == "no usable tools"

    def test_spawning_disabled_refuses_new_agent_steps(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x"))])
        result = Supervisor(model=model, agents={}, verbose=False, max_subtask_retries=0).run("task")
        assert result.subtasks[0].error == "spawn refused: spawning is disabled"

    def test_the_spawn_limit_applies_across_the_plan(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]),
                                        new_agent_step("s2", "b", "y", ["web"]))],
                       sub=lambda m: make_final("x"))
        result = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1)).run("task")
        assert result.subtasks[0].error is None
        assert result.subtasks[1].error == "spawn refused: spawn limit reached"

    def test_a_skipped_step_does_not_spend_a_spawn(self):
        model = router(plans=[plan_json(
            step("s1", "bad"), new_agent_step("s2", "a", "x", ["web"], deps=["s1"]))],
            sub=lambda m: make_final("x"))
        bad = ScriptedRunner(("nope", "stuck"))
        s = sup(model, {"bad": ("b", bad)})
        result = s.run("task")
        assert result.subtasks[1].skipped and result.spawned == []

    def test_a_new_agent_named_spawn_fails_the_run_instead_of_vanishing(self):
        bad = plan_json({"id": "s1", "query": "q",
                         "new_agent": {"name": "__spawn__", "instructions": "Be useful.", "tools": ["web"]}})
        model = router(plans=[bad, bad], sub=lambda m: make_final("x"))
        result = sup(model).run("task")
        assert len(model.planner_prompts()) == 2                 # the repair retry happened
        assert "name is reserved" in model.planner_prompts()[1]
        assert result.content == "Supervisor failed to produce a valid plan."
        assert result.outcome == "stuck" and result.subtasks == [] and result.spawned == []

    def test_a_malformed_definition_triggers_the_plan_repair_retry(self):
        bad = plan_json({"id": "s1", "query": "q", "new_agent": {"name": "a"}})
        good = plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))
        model = router(plans=[bad, good], sub=lambda m: make_final("x"))
        result = sup(model).run("task")
        prompts = model.planner_prompts()
        assert len(prompts) == 2 and "new_agent invalid" in prompts[1] and "must not be combined with agent" in prompts[1]
        assert result.subtasks[0].agent == "a" and result.subtasks[0].content == "x"


class TestLegacySpawnStep:
    def test_the_3_5_spawn_step_still_works_and_nothing_leaks(self, tmp_path):
        cfg = SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)])
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "reads files",
             "capabilities": ["files"]},
            step("s1", "scout", "look"))], sub=lambda m: make_final("scouted"))
        s = sup(model, cfg=cfg)
        result = s.run("task")
        assert result.subtasks[0].agent == "__spawn__"
        assert result.subtasks[0].content.startswith("registered new specialist 'scout'")
        assert result.subtasks[1].agent == "scout" and result.subtasks[1].content == "scouted"
        assert "scout" not in s.agents

    def test_a_refused_legacy_spawn_is_reported(self):
        cfg = SpawnConfig(enabled=True, approver=lambda r: False)
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]})])
        result = sup(model, cfg=cfg).run("task")
        assert result.subtasks[0].error == "spawn refused (see log for reason)"

    def test_handle_spawn_called_directly_registers_on_the_instance(self, tmp_path):
        s = Supervisor(model=router(), agents={}, verbose=False,
                       spawn_config=SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)]))
        name, rewrite = s._handle_spawn({"name": "scout", "description": "reads", "capabilities": ["files"]})
        assert (name, rewrite) == ("scout", None) and "scout" in s.agents
        assert s._handle_spawn({"name": "", "description": "d"}) == (None, None)


class TestPerRunRegistry:
    def test_closing_the_stream_early_leaves_the_supervisor_and_its_runners_as_they_were(self):
        worker = AgentRunner(model=router(), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "worker", deps=["s1"]))],
                       sub=lambda m: make_final("x"), agent=lambda m: make_final("w"))
        worker.model = model
        s = sup(model, {"worker": ("w", worker)})
        before = dict(s.agents)
        gen = s.stream("task")
        for ev in gen:
            if ev["type"] == "spawn":
                break
        gen.close()
        assert s.agents == before and "a" not in s.agents
        assert "delegate" not in worker.registry.names

    def test_the_registry_is_restored_when_a_run_raises(self):
        class Boom(ScriptedRunner):
            def Initialize(self, query, _budget=None):
                raise KeyboardInterrupt

        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "boom", deps=["s1"]))],
                       sub=lambda m: make_final("x"))
        s = sup(model, {"boom": ("b", Boom())})
        before = dict(s.agents)
        with pytest.raises(KeyboardInterrupt):
            s.run("task")
        assert s.agents == before

    def test_consecutive_runs_do_not_share_spawns(self):
        plans = [plan_json(new_agent_step("s1", "a", "x", ["web"]))] * 2
        model = router(plans=plans, sub=lambda m: make_final("x"))
        s = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        assert s.run("one").spawned[0]["name"] == "a"
        second = s.run("two")
        assert second.subtasks[0].error is None and len(second.spawned) == 1


class TestSpecialistsDelegate:
    def test_specialists_have_delegate_during_the_run_and_lose_it_after(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        sup(model, {"worker": ("w", worker)}).run("task")
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
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        s = sup(model, {"worker": ("w", worker)})
        evs = events_of(s)
        done = [e for e in evs if e["type"] == "completion"][0]["result"]
        assert done.subtasks[0].content == "worker done"
        assert done.spawned == [{"name": "delegate_1", "origin": "delegate", "tools": ["web"],
                                 "dropped": [], "outcome": "done", "chars": 7}]
        kinds = [e["type"] for e in evs]
        assert kinds.index("spawn") < kinds.index("delegate_result") < kinds.index("subtask_result")

    def test_no_spawn_config_means_no_delegate_and_no_prompt_text(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = Supervisor(model=model, agents={"worker": ("w", worker)}, verbose=False).run("task")
        assert seen == [False] and result.spawned == []


class TestPersistentDefault:
    def test_a_persistent_supervisor_gets_the_safe_ceiling_by_default(self):
        s = Supervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5))
        cfg = s.spawn_config
        assert cfg.enabled and cfg.capabilities == {"web", "files_read"} and cfg.effective_max_spawns == 6

    def test_an_explicit_config_always_wins(self):
        s = Supervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5),
                       spawn_config=SpawnConfig(enabled=False))
        assert not s.spawn_config.enabled

    def test_without_persistence_the_default_is_off(self):
        assert not Supervisor(model=router(), agents={}, verbose=False).spawn_config.enabled
