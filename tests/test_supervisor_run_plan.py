"""_run_plan: the execution block, callable on its own."""

import asyncio
from types import SimpleNamespace

from agentx_dev.Supervisor import AsyncSupervisor, Supervisor
from tests.conftest import MockModel


class Runner:
    tools = []

    def Initialize(self, query):
        return SimpleNamespace(content="scraped")


class AsyncRunner:
    tools = []

    async def Initialize(self, query):
        return SimpleNamespace(content="scraped")


PLAN = [{"id": "a", "agent": "worker", "query": "q"}]


def test_sync_run_plan_streams_events_and_fills_the_shared_containers():
    sup = Supervisor(model=MockModel(script=[]), agents={"worker": ("w", Runner())}, verbose=False)
    results, by_id = [], {}
    events = list(sup._run_plan(PLAN, results, by_id))
    assert [e["type"] for e in events] == ["dispatch", "subtask_result"]
    assert [r.step_id for r in results] == ["a"] and by_id["a"].content == "scraped"


def test_async_early_exit_cancels_running_subtasks_before_aclose_returns():
    """Closing astream early must cancel and await in-flight sub-tasks (no deferred GC cleanup)."""
    import asyncio as _asyncio
    from tests.conftest import MockModel as _Model
    import json as _json

    started = []
    finished = []

    class Fast:
        tools = []

        async def Initialize(self, query):
            return SimpleNamespace(content="fast")

    class Slow:
        tools = []

        async def Initialize(self, query):
            started.append(1)
            try:
                await _asyncio.sleep(30)
            except _asyncio.CancelledError:
                finished.append("cancelled")
                raise
            return SimpleNamespace(content="slow")

    plan = _json.dumps({"plan": [{"id": "a", "agent": "fast", "query": "q"},
                                 {"id": "b", "agent": "slow", "query": "q"}]})
    sup = AsyncSupervisor(model=_Model(script=[plan, "final"]),
                          agents={"fast": ("f", Fast()), "slow": ("s", Slow())}, verbose=False)

    async def scenario():
        gen = sup.astream("task")
        async for event in gen:
            if event["type"] == "subtask_result":
                break
        await gen.aclose()
        return list(finished)          # must already be populated when aclose() returns

    assert asyncio.run(scenario()) == ["cancelled"]


def test_async_run_plan_streams_events_and_fills_the_shared_containers():
    sup = AsyncSupervisor(model=MockModel(script=[]), agents={"worker": ("w", AsyncRunner())}, verbose=False)
    results, by_id = [], {}

    async def collect():
        return [e async for e in sup._run_plan(PLAN, results, by_id)]

    events = asyncio.run(collect())
    assert [e["type"] for e in events] == ["dispatch", "subtask_result"]
    assert [r.step_id for r in results] == ["a"] and by_id["a"].content == "scraped"
