"""Regressions for seven correctness flaws in the agent loop.

Each test reproduces a defect that shipped: the assertion here failed
before the fix and passes after. One class per flaw, so a failure names
the specific behaviour that came back.
"""

import asyncio

from agentx_dev import (
    AgentRunner, AsyncAgentRunner, AgentType, StandardTool, AsyncStandardTool,
    AsyncSupervisor,
)
from agentx_dev.Supervisor import SubtaskResult
from tests.conftest import MockModel, make_react_response, make_final


def _tool(fn=None, name="weather"):
    return StandardTool(func=fn or (lambda q: "sunny"), name=name,
                        description="Weather.")


def _use(name, args, cid="c1"):
    return {"type": "tool_use", "name": name, "input": args, "id": cid}


def _respond(answer, cid="c9"):
    return _use("respond", {"answer": answer}, cid)


class TestNativeSpiralGuard:
    """The loop-level repeat breaker must also cover native binding.

    It used to sit after the native branch's ``continue``, so a model
    stuck re-issuing one call ran to max_iterations -- 20 LLM turns
    where text mode stopped at 3.
    """

    def test_identical_native_turns_force_stop(self):
        model = MockModel(tool_script=[_use("weather", {"input": "Accra"})] * 50)
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_tool()],
                             max_iterations=20, bind_tools_natively=True,
                             verbose=False)
        result = runner.invoke("weather?")
        assert len(model.tool_calls_made) <= 4, "spiral ran past the force-stop"
        assert "Terminated" in result.content

    def test_text_mode_guard_still_works(self):
        model = MockModel(script=[make_react_response("weather", "Accra")] * 50)
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_tool()],
                             max_iterations=20, verbose=False)
        assert "Terminated" in runner.invoke("weather?").content


class TestRespondBatchedWithTools:
    """``respond`` alongside real tool calls must not discard them."""

    def test_batched_tool_still_runs(self):
        calls = []
        turn = {
            "type": "tool_use",
            "tool_calls": [
                {"name": "weather", "input": {"input": "Accra"}, "id": "c1"},
                {"name": "respond", "input": {"answer": "It is sunny."}, "id": "c2"},
            ],
            "name": "weather", "input": {"input": "Accra"}, "id": "c1",
        }
        model = MockModel(tool_script=[turn, _respond("It is sunny.")])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[_tool(lambda q: (calls.append(q), "sunny")[1])],
            max_iterations=5, bind_tools_natively=True, verbose=False,
        )
        result = runner.invoke("weather?")
        assert calls == ["Accra"], "the batched tool call was dropped"
        assert [t.name for t in result.tool_calls] == ["weather"]
        assert result.content == "It is sunny."


class TestHistoryRoundTrip:
    """``completion.history`` must be replayable as ``chat_history``.

    The old copy filter kept only truthy {role, content}: tool-calling
    assistant turns (content="") vanished, role="tool" lost its
    tool_call_id, and a stored system prompt was replayed on top of the
    freshly built one.
    """

    def test_replay_is_well_formed(self):
        first_model = MockModel(tool_script=[
            _use("weather", {"input": "Accra"}), _respond("sunny", "c2"),
        ])
        first = AgentRunner(model=first_model, agent=AgentType.ReAct,
                            tools=[_tool()], max_iterations=5,
                            bind_tools_natively=True,
                            verbose=False).invoke("weather?")

        model = MockModel(tool_script=[_respond("ok")])
        AgentRunner(model=model, agent=AgentType.ReAct, tools=[_tool()],
                    max_iterations=3, bind_tools_natively=True,
                    verbose=False).invoke("and tomorrow?", chat_history=first.history)
        sent = model.tool_calls_made[0]

        assert sum(1 for m in sent if m.get("role") == "system") == 1, \
            "the stored system prompt was replayed on top of the fresh one"
        assert any(m.get("role") == "assistant" for m in sent), \
            "assistant tool-calling turns were dropped from history"
        assert not [m for m in sent
                    if m.get("role") == "tool" and not m.get("tool_call_id")], \
            "tool message lost its tool_call_id"


class TestAsyncToolOnSyncRunner:
    """``known_tools`` must union all four registry tables.

    A sync runner accepted an async tool, listed it, and advertised it in
    the prompt -- then treated the call as unknown and returned
    action_input as the final answer with no error.
    """

    def test_async_tool_is_recognised(self):
        async def afetch(q):
            return "async-result"

        atool = AsyncStandardTool(func=afetch, name="afetch",
                                  description="Async fetch.")
        model = MockModel(script=[
            make_react_response("afetch", "x"), make_final("done"),
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[_tool(), atool], max_iterations=4,
                             verbose=False)
        known = (set(runner.registry.sync_std) | set(runner.registry.sync_struct)
                 | set(runner.registry.async_std)
                 | set(runner.registry.async_struct))
        assert "afetch" in known
        assert runner.invoke("fetch x").content != "x", \
            "async tool fell through to implicit-final (silent wrong answer)"


class TestAsyncFunctionCallingShape:
    """AsyncAgentRunner must record FC turns as tool calls, not prose."""

    def test_assistant_turn_carries_tool_calls(self):
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "React_", "id": "fc1",
             "input": {"Thought": "t", "action": "weather", "action_input": "A"}},
            {"type": "tool_use", "name": "React_", "id": "fc2",
             "input": {"Thought": "t", "action": "Final_Answer",
                       "action_input": "sunny"}},
        ])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct,
                                  tools=[_tool()], max_iterations=5,
                                  use_function_calling=True, verbose=False)
        history = asyncio.run(runner.ainvoke("weather?")).history
        assert any(m.get("tool_calls") for m in history), \
            "FC turn recorded as raw JSON text instead of a tool call"
        assert any(m.get("role") == "tool" and m.get("tool_call_id")
                   for m in history), "tool result was not correlated by id"


class TestSchedulerOwnsItsTasks:
    """A BaseException must not leave sibling specialists running.

    CancelledError and KeyboardInterrupt are not Exception, so
    ``_run_subtask``'s handler never sees them; without a try/finally the
    siblings kept running unowned -- in-flight LLM calls still billing.
    """

    def test_siblings_are_cancelled(self):
        finished = {"n": 0}
        plan = [
            {"id": "a", "agent": "slow", "query": "q1", "depends_on": []},
            {"id": "b", "agent": "boom", "query": "q2", "depends_on": []},
        ]

        class Sup(AsyncSupervisor):
            async def _plan(self, task):
                return plan

            async def _synthesize(self, task, subs):
                return "synth"

            async def _run_subtask(self, agent, query, prior_results=None):
                if agent == "boom":
                    await asyncio.sleep(0.01)
                    raise BaseException("hard failure")
                await asyncio.sleep(0.20)
                finished["n"] += 1
                return SubtaskResult(agent=agent, query=query, content="ok")

        sup = Sup(model=object(),
                  agents={"slow": ("s", None), "boom": ("b", None)},
                  max_parallel=4, verbose=False)

        async def main():
            try:
                await sup.run("task")
            except BaseException:
                pass
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            await asyncio.sleep(0.35)
            return len(pending), finished["n"]

        pending, kept_running = asyncio.run(main())
        assert pending == 0, "sibling task was orphaned"
        assert kept_running == 0, "orphaned specialist ran to completion unowned"


class TestToolCacheIsolation:
    """The process-wide tool cache was keyed on (name, args) only.

    Two runners whose tools merely share a name -- routine across
    Supervisor specialists -- served each other's results.
    """

    @staticmethod
    def _run(tool):
        model = MockModel(tool_script=[
            _use("search", {"input": "Q"}), _respond("done"),
        ])
        completion = AgentRunner(model=model, agent=AgentType.ReAct,
                                 tools=[tool], bind_tools_natively=True,
                                 verbose=False).invoke("q")
        return completion.tool_calls[0].result if completion.tool_calls else None

    def test_same_name_different_impl_does_not_collide(self):
        def web_search(q):
            return "WEB RESULT"

        def db_search(q):
            return "DB RESULT"

        first = self._run(StandardTool(func=web_search, name="search",
                                       description="Search."))
        second = self._run(StandardTool(func=db_search, name="search",
                                        description="Search."))
        assert first == "WEB RESULT"
        assert second == "DB RESULT", "second agent was served the first's cache"

    def test_identical_tool_still_shares_cache(self):
        calls = []

        def pure(q):
            calls.append(q)
            return "CACHED"

        tool = StandardTool(func=pure, name="pure_tool", description="Pure.")
        for _ in range(2):
            model = MockModel(tool_script=[
                _use("pure_tool", {"input": "Q"}), _respond("d"),
            ])
            AgentRunner(model=model, agent=AgentType.ReAct, tools=[tool],
                        bind_tools_natively=True, verbose=False).invoke("q")
        assert len(calls) == 1, "caching stopped working for an identical tool"
