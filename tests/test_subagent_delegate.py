"""The delegate tool and attach_delegation (sync runners)."""

from types import SimpleNamespace

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, StandardTool, ToolError
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.SubAgents import (
    AgentSpec, Built, SpawnConfig, SpawnPolicy, attach_delegation, cap_summary,
)
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router

CEILING = dict(enabled=True, capabilities={"web"})


def parent(model, cfg=None, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                       delegation=cfg or SpawnConfig(**CEILING), **kw)


def delegating_agent(args):
    """A parent model that delegates on its first turn and finishes on its second."""
    turns = []

    def script(messages):
        turns.append(1)
        return make_react_response("delegate", args) if len(turns) == 1 else make_final("parent done")
    return script


class FakeRunner:
    def __init__(self, content="fake answer", outcome="done", boom=None):
        self.content, self.outcome, self.boom = content, outcome, boom
        self.task = None
        self.budget = None

    def Initialize(self, task, _budget=None):
        self.task, self.budget = task, _budget
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=self.content, outcome=self.outcome)


def fake_build(p, runner, dropped=()):
    def build(spec, depth=1, *, is_async=None):
        return Built(spec=spec, runner=runner, description="d", granted=[], dropped=list(dropped))
    p._delegation.build = build


class TestDelegateEndToEnd:
    def test_a_specialist_hands_work_to_a_fresh_sub_agent_and_gets_the_answer_back(self):
        args = {"task": "find X", "instructions": "You find X. Return it.", "tools": ["web"]}
        model = router(agent=delegating_agent(args), sub=lambda m: make_final("X is 42"))
        p = parent(model)
        result = p.invoke("PARENT-SECRET: do the thing")
        assert result.content == "parent done" and "X is 42" in str(result.history)
        [rec] = p.spawned
        assert rec["name"] == "delegate_1" and rec["origin"] == "delegate"
        assert rec["outcome"] == "done" and rec["chars"] == len("X is 42") and rec["tools"] == ["web"]

    def test_the_sub_agent_sees_its_task_and_role_but_not_the_callers_history(self):
        args = {"task": "find X", "instructions": "You find X. Return it.", "tools": []}
        seen = []
        model = router(agent=delegating_agent(args), sub=lambda m: seen.append(m) or make_final("ok"))
        parent(model).invoke("PARENT-SECRET: do the thing")
        text = str(seen[0])
        assert "find X" in text and "You find X. Return it." in text
        assert "PARENT-SECRET" not in text

    def test_the_summary_is_capped_with_a_marker(self):
        model = router(agent=delegating_agent({"task": "t"}), sub=lambda m: make_final("y" * 9000))
        p = parent(model)
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, str) and len(out) == 4000 + len("\n[truncated]") and out.endswith("[truncated]")
        assert cap_summary("short") == "short"

    def test_clipped_tools_are_reported_back_to_the_caller(self):
        model = router(sub=lambda m: make_final("done"))
        out = parent(model).registry.dispatch("delegate", {"task": "t", "tools": ["web", "code"]})
        assert out.startswith("done") and "[note: these tools were not granted: code]" in out

    def test_events_and_the_spawn_count_reset_for_every_run(self):
        cfg = SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1)
        model = router(sub=lambda m: make_final("ok"))
        p = parent(model, cfg)
        assert p.registry.dispatch("delegate", {"task": "one"}) == "ok"
        events = p._delegation.run.drain()
        assert [e["type"] for e in events] == ["spawn", "delegate_result"]
        assert events[1] == {"type": "delegate_result", "name": "delegate_1", "outcome": "done", "chars": 2}
        refused = p.registry.dispatch("delegate", {"task": "two"})
        assert refused == "delegation refused: spawn limit reached; do this yourself"
        # a new invocation starts a fresh count and a fresh record list
        p.invoke("again")
        assert p._delegation.run.count == 0 and p._delegation.run.records == []
        assert p.registry.dispatch("delegate", {"task": "three"}) == "ok"


class TestRefusalsAndFailures:
    def test_refusals_are_plain_results_not_errors(self):
        p = parent(router())
        assert p.registry.dispatch("delegate", {"task": "   "}) == \
            "delegation refused: task is empty; do this yourself"
        off = parent(router(), SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=0))
        out = off.registry.dispatch("delegate", {"task": "t"})
        assert out == "delegation refused: spawn limit reached; do this yourself"
        none = parent(router()).registry.dispatch("delegate", {"task": "t", "tools": ["code"]})
        assert none == "delegation refused: no usable tools; do this yourself"

    def test_a_sub_agent_that_gave_up_raises_a_tool_error_with_its_report(self):
        p = parent(router())
        fake_build(p, FakeRunner("tried A, tried B", outcome="stuck"))
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, ToolError)
        assert "[delegate failed: stuck] tried A, tried B" in str(out)
        events = p._delegation.run.drain()
        assert events[-1] == {"type": "delegate_result", "name": "delegate_1", "outcome": "stuck", "chars": 16}

    def test_a_sub_agent_that_raises_is_a_tool_error(self):
        p = parent(router())
        fake_build(p, FakeRunner(boom=RuntimeError("provider exploded")))
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, ToolError) and "[delegate failed: error] provider exploded" in str(out)

    def test_a_stuck_delegation_does_not_end_the_callers_run(self):
        args = {"task": "t"}
        model = router(agent=delegating_agent(args))
        p = parent(model)
        fake_build(p, FakeRunner("nope", outcome="stuck"))
        result = p.invoke("go")
        assert result.outcome == "done" and result.content == "parent done"
        assert "[delegate failed: stuck] nope" in str(result.history)


class TestPersistenceAndBudget:
    def test_the_sub_agent_shares_the_callers_deadline(self):
        args = {"task": "t"}
        model = router(agent=delegating_agent(args))
        p = parent(model, persistence=Persistence(max_minutes=5))
        active = []

        class Watching(FakeRunner):
            def Initialize(self, task, _budget=None):
                active.append(p._active_budget)          # the caller's budget while it runs
                return FakeRunner.Initialize(self, task, _budget)

        fake = Watching("ok")
        fake_build(p, fake)
        p.invoke("go")
        assert isinstance(active[0], RunBudget)
        assert fake.budget is active[0]                  # the very same budget, not a fresh one

    def test_without_persistence_no_budget_is_passed(self):
        p = parent(router(agent=delegating_agent({"task": "t"})))
        fake = FakeRunner("ok")
        fake_build(p, fake)
        p.invoke("go")
        assert fake.budget is None

    def test_built_sub_agents_inherit_the_callers_persistence(self):
        persistence = Persistence(max_minutes=5)
        model = router(agent=delegating_agent({"task": "t"}), sub=lambda m: make_final("ok"))
        p = parent(model, persistence=persistence)
        p.invoke("go")
        assert p._delegation.persistence is persistence
        assert p._delegation.run.built["delegate_1"].runner.persistence is persistence


class TestDepth:
    def test_depth_decides_whether_a_built_sub_agent_can_delegate(self):
        p = SpawnPolicy(SpawnConfig(max_depth=2, **CEILING), MockModel())
        assert "delegate" in p.build(AgentSpec("a", "x", ("web",)), depth=1).runner.registry.names
        assert "delegate" not in p.build(AgentSpec("b", "x", ("web",)), depth=2).runner.registry.names

    def test_the_default_depth_gives_a_sub_agent_no_delegate(self):
        p = SpawnPolicy(SpawnConfig(**CEILING), MockModel())
        assert "delegate" not in p.build(AgentSpec("a", "x", ("web",)), depth=1).runner.registry.names


class TestWiring:
    def test_no_config_or_a_disabled_config_means_no_delegate_tool(self):
        plain = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        off = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False,
                          delegation=SpawnConfig(enabled=False))
        assert "delegate" not in plain.registry.names and "delegate" not in off.registry.names
        assert plain.spawned == []

    def test_a_delegation_is_never_answered_from_the_tool_cache(self):
        from agentx_dev import InMemoryCache
        answers = iter(["one", "two"])
        p = parent(router(sub=lambda m: make_final(next(answers))))
        p.registry.configure_cache(InMemoryCache())
        assert p.registry.dispatch("delegate", {"task": "same"}) == "one"
        assert p.registry.dispatch("delegate", {"task": "same"}) == "two"

    def test_the_tool_description_carries_the_menu_of_grantable_tools(self):
        tool = parent(MockModel()).registry._tool_by_name["delegate"]
        assert "web:" in tool.description and "files:" not in tool.description


    def test_a_pool_tool_named_delegate_with_depth_to_spare_is_a_refusal_not_a_crash(self):
        from agentx_dev.SubAgents import SpawnRefused
        clash = StandardTool(name="delegate", description="clash", func=lambda: "x")
        p = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}, tools=[clash], max_depth=2), MockModel())
        with pytest.raises(SpawnRefused) as exc:
            p.build(AgentSpec("a", "x", ("delegate",)), depth=1)
        assert exc.value.reason.startswith("could not build")

class TestAttachDelegation:
    def test_adds_then_removes_and_skips_runners_that_already_have_it(self):
        p = SpawnPolicy(SpawnConfig(**CEILING), MockModel())
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        b = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False,
                        delegation=SpawnConfig(**CEILING))          # already has its own delegate
        own = b.registry._tool_by_name["delegate"]
        plain = object()                                            # no add_tool: ignored
        with attach_delegation([a, b, plain, a], p):
            assert "delegate" in a.registry.names and a.registry.names.count("delegate") == 1
            assert b.registry._tool_by_name["delegate"] is own
        assert "delegate" not in a.registry.names
        assert b.registry._tool_by_name["delegate"] is own           # the runner's own tool stays

    def test_not_attached_when_disabled_in_legacy_mode_or_at_max_depth(self):
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        with attach_delegation([a], SpawnPolicy(SpawnConfig(enabled=False), MockModel())):
            assert "delegate" not in a.registry.names
        legacy = SpawnConfig(enabled=True, auto_spawn=True)                 # no capabilities= / tools=
        with attach_delegation([a], SpawnPolicy(legacy, MockModel())):
            assert "delegate" not in a.registry.names
        with attach_delegation([a], SpawnPolicy(SpawnConfig(enabled=True, tools=[]), MockModel())):
            assert "delegate" in a.registry.names                          # any ceiling counts
        with attach_delegation([a], SpawnPolicy(SpawnConfig(**CEILING), MockModel()), depth=1):
            assert "delegate" not in a.registry.names              # depth 1 is not < max_depth 1

    def test_removed_even_when_the_block_raises(self):
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        with pytest.raises(RuntimeError):
            with attach_delegation([a], SpawnPolicy(SpawnConfig(**CEILING), MockModel())):
                raise RuntimeError("boom")
        assert "delegate" not in a.registry.names
