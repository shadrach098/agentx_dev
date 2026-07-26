"""The proactive 'act, don't announce' system-prompt clause.

Complements the reactive text-turn nudge: a tool-using agent is told up
front to call the tool rather than narrate the intention. Only injected
when the agent actually has tools — a tool-less chat agent should answer
in prose, so the clause must NOT appear there.
"""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, AgentType, StandardTool
from tests.conftest import MockModel, make_final

MARKER = "ACT, DON'T ANNOUNCE"


def a_tool():
    return StandardTool(func=lambda x: "ok", name="set_goal",
                        description="Persist the athlete's goal.")


def _system_text(messages):
    return next((m["content"] for m in messages if m.get("role") == "system"), "")


class TestSync:

    def test_clause_present_when_tools_registered(self):
        model = MockModel(script=[make_final("done")])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[a_tool()], verbose=False)
        runner.invoke("set my goal")
        assert MARKER in _system_text(model.calls[0])

    def test_clause_absent_when_no_tools(self):
        model = MockModel(script=[make_final("Paris.")])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[], verbose=False)
        runner.invoke("capital of France?")
        assert MARKER not in _system_text(model.calls[0])

    def test_clause_present_in_native_mode(self):
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "respond",
             "input": {"answer": "done"}, "id": "c1"},
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[a_tool()], verbose=False,
                             bind_tools_natively=True)
        runner.invoke("set my goal")
        assert MARKER in _system_text(model.tool_calls_made[0])


class TestAsync:

    def test_clause_present_when_tools_registered(self):
        model = MockModel(script=[make_final("done")])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct,
                                  tools=[a_tool()], verbose=False)
        asyncio.run(runner.ainvoke("set my goal"))
        assert MARKER in _system_text(model.calls[0])

    def test_clause_absent_when_no_tools(self):
        model = MockModel(script=[make_final("Paris.")])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct,
                                  tools=[], verbose=False)
        asyncio.run(runner.ainvoke("capital of France?"))
        assert MARKER not in _system_text(model.calls[0])
