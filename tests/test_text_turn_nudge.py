"""Tests for the text-only-turn nudge.

A model that announces an intention without acting ("I'll look up your
recent scores. Just a second!") used to have that preamble promoted
straight to the final answer, because every "no tool call found" branch
in the loop was coded as "this text is the answer -- break".

The runner now feeds one nudge back and lets the model actually act.
Bounded by ``text_turn_nudges`` so a model that genuinely wants to answer
in prose costs at most one extra call.
"""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, AgentType, StandardTool
from tests.conftest import MockModel

PREAMBLE = "I'll look up your recent scores to get a clear view of your communication skills. Just a second!"
REAL_ANSWER = "Your communication score is 82, up 6 points."


def build_scores_tool():
    return StandardTool(
        func=lambda x: "communication: 82",
        name="get_my_scores",
        description="Fetch the athlete's recent scores.",
    )


class TestTextMode:

    def test_prose_preamble_gets_nudged_not_returned(self, make_final_response):
        model = MockModel(script=[PREAMBLE, make_final_response(REAL_ANSWER)])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == REAL_ANSWER
        assert "Just a second" not in result.content
        assert len(model.calls) == 2, "runner should have re-prompted once"

    def test_nudge_is_visible_to_the_model(self, make_final_response):
        model = MockModel(script=[PREAMBLE, make_final_response(REAL_ANSWER)])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
        )
        runner.invoke("How am I doing?")
        second_call = model.calls[1]
        assert any(
            "[framework]" in str(m.get("content", "")) for m in second_call
        ), "the nudge should be appended to the history the model sees"

    def test_budget_exhausted_surfaces_the_text(self):
        # Model insists on prose. After the single allowed nudge, the runner
        # stops burning calls and surfaces what it has.
        model = MockModel(script=[PREAMBLE, PREAMBLE, PREAMBLE])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == PREAMBLE
        assert len(model.calls) == 2, "one nudge, then accept"

    def test_nudges_disabled_restores_old_behavior(self):
        model = MockModel(script=[PREAMBLE, "unused"])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False, text_turn_nudges=0,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == PREAMBLE
        assert len(model.calls) == 1

    def test_no_tools_registered_means_no_nudge(self):
        # Plain-chat use: prose IS the answer. Don't waste a call.
        model = MockModel(script=["Paris is the capital.", "unused"])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        result = runner.invoke("Capital of France?")
        assert result.content == "Paris is the capital."
        assert len(model.calls) == 1


class TestNativeBindingMode:

    def test_text_only_turn_gets_nudged(self):
        model = MockModel(tool_script=[
            {"type": "text", "text": PREAMBLE},
            {"type": "tool_use", "name": "respond",
             "input": {"answer": REAL_ANSWER}, "id": "c1"},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
            bind_tools_natively=True,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2

    def test_tool_call_after_nudge_still_dispatches(self):
        model = MockModel(tool_script=[
            {"type": "text", "text": PREAMBLE},
            {"type": "tool_use", "name": "get_my_scores",
             "input": {"input": "me"}, "id": "c1"},
            {"type": "tool_use", "name": "respond",
             "input": {"answer": REAL_ANSWER}, "id": "c2"},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
            bind_tools_natively=True, max_iterations=5,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == REAL_ANSWER
        assert [tc.name for tc in result.tool_calls] == ["get_my_scores"]


class TestFunctionCallingMode:

    def test_text_only_turn_gets_nudged(self):
        model = MockModel(tool_script=[
            {"type": "text", "text": PREAMBLE},
            {"type": "tool_use", "name": "ReAct", "id": "c1", "input": {
                "Thought": "I have the scores.",
                "action": "Final_Answer",
                "action_input": REAL_ANSWER,
            }},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
            use_function_calling=True,
        )
        result = runner.invoke("How am I doing?")
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2


class TestAsyncRunner:

    def test_text_mode_nudged(self, make_final_response):
        model = MockModel(script=[PREAMBLE, make_final_response(REAL_ANSWER)])
        runner = AsyncAgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
        )
        result = asyncio.run(runner.ainvoke("How am I doing?"))
        assert result.content == REAL_ANSWER
        assert len(model.calls) == 2

    def test_native_mode_nudged(self):
        model = MockModel(tool_script=[
            {"type": "text", "text": PREAMBLE},
            {"type": "tool_use", "name": "respond",
             "input": {"answer": REAL_ANSWER}, "id": "c1"},
        ])
        runner = AsyncAgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
            bind_tools_natively=True,
        )
        result = asyncio.run(runner.ainvoke("How am I doing?"))
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2

    def test_function_calling_mode_nudged(self):
        model = MockModel(tool_script=[
            {"type": "text", "text": PREAMBLE},
            {"type": "tool_use", "name": "ReAct", "id": "c1", "input": {
                "Thought": "done",
                "action": "Final_Answer",
                "action_input": REAL_ANSWER,
            }},
        ])
        runner = AsyncAgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_scores_tool()], verbose=False,
            use_function_calling=True,
        )
        result = asyncio.run(runner.ainvoke("How am I doing?"))
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2
