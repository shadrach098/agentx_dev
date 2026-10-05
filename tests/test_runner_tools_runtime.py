"""Runner support for sub-agents: add/remove tools at run time, async system_addendum, run budget."""

import asyncio

import pytest

from agentx_dev import (
    AgentRunner, AgentType, AsyncAgentRunner, AsyncStandardTool, Persistence, StandardTool,
)
from tests.conftest import MockModel, make_final, make_react_response


def echo(x):
    return f"echo {x}"


ECHO = StandardTool(func=echo, name="echo", description="echoes")
LATE = StandardTool(func=lambda x: f"late {x}", name="late", description="added later")


def sync_runner(model, tools=(ECHO,), **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False, **kw)


def async_runner(model, tools=(ECHO,), **kw):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False, **kw)


class TestRegistryUnregister:
    def test_unregister_removes_every_trace(self):
        r = sync_runner(MockModel())
        r.registry.unregister("echo")
        assert not r.registry.has("echo") and r.registry.names == []
        assert "echo" not in r.registry.names_block()
        r.registry.unregister("echo")          # unknown names are ignored


class TestSyncAddRemove:
    def test_added_tool_is_dispatchable_and_listed_in_the_prompt(self):
        model = MockModel(script=[make_react_response("late", "x"), make_final("done")])
        r = sync_runner(model)
        r.add_tool(LATE)
        assert r.registry.has("late") and "late" in r._tool_names_block and LATE in r.tools
        result = r.invoke("go")
        assert "late x" in str(result.history)
        assert "late" in str(model.calls[0][0]["content"])      # the system prompt lists it

    def test_remove_restores_the_original_tools_and_does_not_mutate_the_callers_list(self):
        mine = [ECHO]
        r = sync_runner(MockModel(), tools=mine)
        r.add_tool(LATE)
        r.remove_tool("late")
        assert r.registry.names == ["echo"] and [t.name for t in r.tools] == ["echo"]
        assert mine == [ECHO]
        assert "late" not in r._tool_names_block

    def test_adding_a_duplicate_name_raises(self):
        r = sync_runner(MockModel())
        with pytest.raises(ValueError, match="already registered"):
            r.add_tool(StandardTool(func=echo, name="echo", description="dup"))


class TestAsyncAddRemove:
    def test_async_runner_add_and_remove(self):
        async def alate(x):
            return f"alate {x}"
        tool = AsyncStandardTool(func=alate, name="alate", description="async late")
        model = MockModel(script=[make_react_response("alate", "x"), make_final("done")])
        r = async_runner(model)
        r.add_tool(tool)
        assert r.registry.has("alate") and "alate" in r._tool_names_block
        result = asyncio.run(r.ainvoke("go"))
        assert "alate x" in str(result.history)
        r.remove_tool("alate")
        assert r.registry.names == ["echo"] and "alate" not in r._tool_names_block

    def test_adding_a_duplicate_name_raises(self):
        with pytest.raises(ValueError, match="already registered"):
            async_runner(MockModel()).add_tool(StandardTool(func=echo, name="echo", description="dup"))


class TestAsyncSystemAddendum:
    def test_the_addendum_reaches_the_system_prompt_after_the_act_line(self):
        model = MockModel(script=[make_final("ok")])
        r = async_runner(model, system_addendum="ROLE: be brief and cite sources")
        asyncio.run(r.ainvoke("hi"))
        system = str(model.calls[0][0]["content"])
        assert system.rstrip().endswith("ROLE: be brief and cite sources")

    def test_without_an_addendum_nothing_is_appended(self):
        model = MockModel(script=[make_final("ok")])
        asyncio.run(async_runner(model).ainvoke("hi"))
        assert "ROLE:" not in str(model.calls[0][0]["content"])


class TestActiveBudget:
    def test_it_is_none_outside_a_run_and_the_runs_budget_inside_one(self):
        seen = []
        box = {}

        def peek(x):
            seen.append(box["runner"]._active_budget)
            return "ok"

        tool = StandardTool(func=peek, name="peek", description="peeks")
        model = MockModel(script=[make_react_response("peek", "x"), make_final("done")])
        r = sync_runner(model, tools=(tool,), persistence=Persistence(max_minutes=5))
        box["runner"] = r
        assert r._active_budget is None
        r.invoke("go")
        assert len(seen) == 1 and seen[0] is not None and seen[0].remaining() > 0
        assert r._active_budget is None

    def test_async_runner_sets_and_clears_it_too(self):
        seen = []
        box = {}

        async def peek(x):
            seen.append(box["runner"]._active_budget)
            return "ok"

        tool = AsyncStandardTool(func=peek, name="peek", description="peeks")
        model = MockModel(script=[make_react_response("peek", "x"), make_final("done")])
        r = async_runner(model, tools=(tool,), persistence=Persistence(max_minutes=5))
        box["runner"] = r
        asyncio.run(r.ainvoke("go"))
        assert len(seen) == 1 and seen[0] is not None
        assert r._active_budget is None


class TestCacheableOptOut:
    def _counting_tool(self, cacheable):
        calls = []
        tool = StandardTool(func=lambda x: calls.append(x) or f"ran {len(calls)}",
                            name="side", description="may have side effects")
        if cacheable is not None:
            tool.cacheable = cacheable
        return tool, calls

    def test_a_tool_marked_not_cacheable_runs_every_time_even_with_the_cache_on(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(False)
        r = sync_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert r.registry.dispatch("side", "same") == "ran 2"
        assert len(calls) == 2

    def test_an_ordinary_tool_is_still_cached(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(None)
        r = sync_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert len(calls) == 1

    def test_the_async_dispatch_honours_the_opt_out(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(False)
        r = async_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert asyncio.run(r.registry.adispatch("side", "same")) == "ran 1"
        assert asyncio.run(r.registry.adispatch("side", "same")) == "ran 2"
