"""Text-mode tool observations must not use role='function'.

Newer OpenAI models (gpt-5.x) reject the legacy `role: "function"`
message type outright ("does not support 'function' with this model"),
and Anthropic never accepted it. In text mode (no use_function_calling)
the runner has no tool_call_id handshake, so it must feed tool results
back as a plain `role: "user"` observation — which every provider and
model generation accepts.

Function-calling mode keeps the native `role: "tool"` + tool_call_id
shape, which modern OpenAI requires and correlates correctly.
"""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, AgentType, StandardTool
from tests.conftest import MockModel, make_react_response, make_final


def calc():
    return StandardTool(func=lambda x: f"= {x}", name="calc",
                        description="Compute an expression.")


def _roles(messages):
    return [m.get("role") for m in messages]


class TestTextMode:

    def test_observation_is_user_not_function(self):
        model = MockModel(script=[
            make_react_response("calc", "2+2"),
            make_final("4"),
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[calc()], verbose=False)
        runner.invoke("compute 2+2")
        # The 2nd LLM call carries the tool result. It must not be role=function.
        second = model.calls[1]
        assert "function" not in _roles(second), \
            "text-mode observation must not use the legacy role='function'"
        assert any(
            m.get("role") == "user" and "Observation" in str(m.get("content", ""))
            for m in second
        ), "tool result should be fed back as a user Observation turn"

    def test_error_observation_is_also_user(self):
        # A tool that raises → error observation must also avoid role=function.
        boom = StandardTool(func=lambda x: (_ for _ in ()).throw(ValueError("boom")),
                            name="boom", description="always fails")
        model = MockModel(script=[
            make_react_response("boom", "x"),
            make_final("done"),
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=[boom], verbose=False)
        runner.invoke("go")
        assert "function" not in _roles(model.calls[1])


class TestFunctionCallingModeUnchanged:

    def test_still_uses_tool_role_with_id(self):
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "ReAct", "id": "call_abc", "input": {
                "Thought": "compute", "action": "calc", "action_input": "2+2",
            }},
            {"type": "tool_use", "name": "ReAct", "id": "call_def", "input": {
                "Thought": "done", "action": "Final_Answer", "action_input": "4",
            }},
        ])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[calc()],
                             verbose=False, use_function_calling=True,
                             max_iterations=5)
        runner.invoke("compute 2+2")
        second = model.tool_calls_made[1]
        # Function-calling keeps the native tool-role handshake.
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "FC mode should feed results as role='tool'"
        assert all(m.get("tool_call_id") for m in tool_msgs)
        assert "function" not in _roles(second)


class TestAsyncTextMode:

    def test_observation_is_user_not_function(self):
        model = MockModel(script=[
            make_react_response("calc", "2+2"),
            make_final("4"),
        ])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct,
                                  tools=[calc()], verbose=False)
        asyncio.run(runner.ainvoke("compute 2+2"))
        second = model.calls[1]
        assert "function" not in _roles(second)
        assert any(
            m.get("role") == "user" and "Observation" in str(m.get("content", ""))
            for m in second
        )
