"""Runner-level guarantees the operator feature relies on: Ctrl-C at a prompt stops a sync
native function-calling run, and a human's answer is never replayed from a cache."""

import pytest

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Operator import ask_human_tool
from tests.conftest import MockModel


def _interrupting_tool():
    def boom(x):
        raise KeyboardInterrupt()

    return StandardTool(func=boom, name="ask_user", description="asks the operator")


class TestKeyboardInterruptStopsTheRun:

    def test_sequential_native_dispatch_propagates(self):
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "ask_user", "id": "c1", "input": {"input": "x"}},
            {"type": "tool_use", "name": "respond", "input": {"answer": "done"}, "id": "c2"},
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_interrupting_tool()],
                             verbose=False, bind_tools_natively=True, max_iterations=5)
        with pytest.raises(KeyboardInterrupt):
            runner.invoke("go")
        assert len(model.tool_calls_made) == 1        # the run stopped, it did not go on to respond

    def test_parallel_native_dispatch_propagates(self):
        other = StandardTool(func=lambda x: "fine", name="lookup", description="looks things up")
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "ask_user", "id": "c1", "input": {"input": "x"},
             "tool_calls": [
                 {"name": "ask_user", "id": "c1", "input": {"input": "x"}},
                 {"name": "lookup", "id": "c2", "input": {"input": "y"}},
             ]},
            {"type": "tool_use", "name": "respond", "input": {"answer": "done"}, "id": "c3"},
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_interrupting_tool(), other],
                             verbose=False, bind_tools_natively=True, max_iterations=5,
                             parallel_tool_workers=2)
        with pytest.raises(KeyboardInterrupt):
            runner.invoke("go")
        assert len(model.tool_calls_made) == 1


class TestAHumanAnswerIsNeverCached:

    def _runner_with_cache(self, tool):
        from agentx_dev import InMemoryCache
        r = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[tool], verbose=False)
        r.registry.configure_cache(InMemoryCache())
        return r

    def test_ask_human_tool_is_not_cacheable(self):
        assert ask_human_tool().cacheable is False

    def test_a_cached_runner_asks_the_human_every_time(self, monkeypatch):
        from agentx_dev import Operator as op
        answers = iter(["no", "yes"])
        asked = []
        monkeypatch.setattr(op, "builtin_asker",
                            lambda q, *, prefix="", timeout=None: asked.append(q) or next(answers))
        r = self._runner_with_cache(ask_human_tool())
        assert r.registry.dispatch("ask_human", {"question": "Apply?"}) == "[operator] no"
        assert r.registry.dispatch("ask_human", {"question": "Apply?"}) == "[operator] yes"
        assert len(asked) == 2

    def test_the_demo_approval_tool_is_not_cacheable(self):
        import importlib.util
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "examples" / "mcp_github_triage_demo.py"
        spec = importlib.util.spec_from_file_location("mcp_github_triage_demo_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ImportError as e:                     # the demo needs the optional mcp extra
            pytest.skip(f"demo imports are not installed: {e}")
        assert mod.approval_tool().cacheable is False
