"""The delegate tool on async runners, including concurrent delegation."""

import asyncio
from types import SimpleNamespace

import pytest

from agentx_dev import AsyncAgentRunner, AgentType, Persistence, ToolError
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.SubAgents import Built, SpawnConfig, SpawnPolicy, attach_delegation
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router

CEILING = dict(enabled=True, capabilities={"web"})


def aparent(model, cfg=None, **kw):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                            delegation=cfg or SpawnConfig(**CEILING), **kw)


def delegating_agent(args):
    turns = []

    def script(messages):
        turns.append(1)
        return make_react_response("delegate", args) if len(turns) == 1 else make_final("parent done")
    return script


class FakeAsyncRunner:
    def __init__(self, content="fake answer", outcome="done", boom=None, barrier=None):
        self.content, self.outcome, self.boom, self.barrier = content, outcome, boom, barrier
        self.budget = None
        self.started = False

    async def Initialize(self, task, _budget=None):
        self.started, self.budget = True, _budget
        if self.barrier is not None:
            await self.barrier.hit()
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=self.content, outcome=self.outcome)


class Barrier:
    """Both delegations must be in flight at once, or the wait times out and the test fails."""

    def __init__(self, n):
        self.n, self.seen, self.evt = n, 0, asyncio.Event()

    async def hit(self):
        self.seen += 1
        if self.seen >= self.n:
            self.evt.set()
        await asyncio.wait_for(self.evt.wait(), 2)


def fake_build(p, runners):
    queue = list(runners)

    def build(spec, depth=1, *, is_async=None):
        return Built(spec=spec, runner=queue.pop(0), description="d", granted=[], dropped=[])
    p._delegation.build = build


class TestAsyncDelegate:
    def test_the_async_runner_gets_an_async_delegate_tool_and_a_sub_agent_answers(self):
        model = router(agent=delegating_agent({"task": "find X", "instructions": "You find X."}),
                       sub=lambda m: make_final("X is 42"))
        p = aparent(model)
        assert p.registry.is_async("delegate")
        result = asyncio.run(p.ainvoke("PARENT-SECRET go"))
        assert result.content == "parent done" and "X is 42" in str(result.history)
        [rec] = p.spawned
        assert rec["name"] == "delegate_1" and rec["outcome"] == "done"
        # the policy built an AsyncAgentRunner for it
        assert isinstance(p._delegation.run.built["delegate_1"].runner, AsyncAgentRunner)

    def test_the_sub_agent_does_not_see_the_callers_history(self):
        seen = []
        model = router(agent=delegating_agent({"task": "find X", "instructions": "You find X."}),
                       sub=lambda m: seen.append(m) or make_final("ok"))
        asyncio.run(aparent(model).ainvoke("PARENT-SECRET go"))
        assert "find X" in str(seen[0]) and "PARENT-SECRET" not in str(seen[0])

    def test_refusals_and_failures_match_the_sync_tool(self):
        p = aparent(router())
        refusal = asyncio.run(p.registry.adispatch("delegate", {"task": " "}))
        assert refusal == "delegation refused: task is empty; do this yourself"
        fake_build(p, [FakeAsyncRunner("gave up", outcome="stuck"), FakeAsyncRunner(boom=RuntimeError("boom"))])
        stuck = asyncio.run(p.registry.adispatch("delegate", {"task": "t"}))
        assert isinstance(stuck, ToolError) and "[delegate failed: stuck] gave up" in str(stuck)
        crashed = asyncio.run(p.registry.adispatch("delegate", {"task": "t"}))
        assert isinstance(crashed, ToolError) and "[delegate failed: error] boom" in str(crashed)

    def test_the_sub_agent_shares_the_callers_deadline(self):
        model = router(agent=delegating_agent({"task": "t"}))
        p = aparent(model, persistence=Persistence(max_minutes=5))
        active = []

        class Watching(FakeAsyncRunner):
            async def Initialize(self, task, _budget=None):
                active.append(p._active_budget)          # the caller's budget while it runs
                return await FakeAsyncRunner.Initialize(self, task, _budget)

        fake = Watching("ok")
        fake_build(p, [fake])
        asyncio.run(p.ainvoke("go"))
        assert isinstance(active[0], RunBudget)
        assert fake.budget is active[0]                  # the very same budget, not a fresh one

    def test_a_sync_parent_under_async_code_still_gets_a_sync_tool(self):
        from agentx_dev import AgentRunner
        sync_parent = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        policy = SpawnPolicy(SpawnConfig(**CEILING), MockModel(), is_async=True)
        with attach_delegation([sync_parent], policy):
            assert not sync_parent.registry.is_async("delegate")


class TestConcurrentDelegation:
    def test_two_delegations_in_one_native_turn_run_at_the_same_time(self):
        calls = [{"name": "delegate", "input": {"task": f"job {i}"}, "id": f"c0_{i}"} for i in range(2)]
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "delegate", "input": {"task": "job 0"}, "tool_calls": calls},
            {"type": "tool_use", "name": "respond", "id": "c9", "input": {"answer": "both done"}},
        ])
        p = aparent(model, bind_tools_natively=True)
        barrier = Barrier(2)
        fake_build(p, [FakeAsyncRunner("A", barrier=barrier), FakeAsyncRunner("B", barrier=barrier)])
        result = asyncio.run(p.ainvoke("go"))
        assert result.content == "both done"
        assert barrier.seen == 2
        text = str([m for m in result.history if m["role"] == "tool"])
        assert "A" in text and "B" in text
