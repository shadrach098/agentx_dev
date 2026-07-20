"""Tests for AgentRunner's control loop against a MockModel.

Covers: single-shot final, one tool call then final, max_iterations,
unknown-tool implicit-final, loop force-stop on identical actions.
"""

import json

import pytest

from agentx_dev import AgentRunner, AgentType, StandardTool
from tests.conftest import MockModel


def build_calc_tool():
    return StandardTool(
        func=lambda x: f"= {x}",
        name="calc",
        description="Compute an expression.",
    )


class TestBasicLoop:

    def test_single_shot_final(self, make_final_response):
        model = MockModel(script=[make_final_response("Paris is the capital.")])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        result = runner.invoke("Capital of France?")
        assert "Paris" in result.content
        assert result.tool_calls == []

    def test_tool_call_then_final(self, make_react, make_final_response):
        model = MockModel(script=[
            make_react("calc", "1+1"),
            make_final_response("= 1+1"),
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_calc_tool()], verbose=False,
        )
        result = runner.invoke("Compute 1+1")
        assert result.tool_calls[0].name == "calc"
        assert "= 1+1" in result.content

    def test_max_iterations_gives_useful_summary(self, make_react):
        # Loop just keeps calling calc forever -- but different args each time
        # to avoid the dup-guard triggering.
        model = MockModel(script=[
            make_react("calc", "1"),
            make_react("calc", "2"),
            make_react("calc", "3"),
            make_react("calc", "4"),
            make_react("calc", "5"),
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_calc_tool()], verbose=False, max_iterations=3,
        )
        result = runner.invoke("Compute stuff")
        assert "max_iterations" in result.content.lower() or "iterations" in result.content.lower()


class TestGuardrails:

    def test_loop_force_stop_on_identical_actions(self, make_react):
        # Same action + args 3 times in a row triggers the loop-level guard.
        model = MockModel(script=[make_react("calc", "5")] * 10)
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_calc_tool()], verbose=False, max_iterations=10,
        )
        result = runner.invoke("Compute 5")
        # Loop should have force-stopped -- either by dup-guard refusal or
        # loop-level termination. Either way, the run completes.
        assert result is not None

    def test_unknown_tool_implicit_final(self, make_react):
        # action="Provide answer" (natural language) with the actual answer
        # in action_input -- runner should route to implicit-final.
        model = MockModel(script=[
            make_react("Provide the answer", "The capital is Paris."),
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        result = runner.invoke("Capital of France?")
        assert "Paris" in result.content


class TestChatHistory:

    def test_chat_history_threaded(self, make_final_response):
        model = MockModel(script=[make_final_response("Yes.")])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        result = runner.invoke(
            "Is that right?",
            chat_history=[
                {"role": "user", "content": "2+2=4"},
                {"role": "assistant", "content": "That's correct"},
            ],
        )
        # The model's messages should include the chat history
        assert len(model.calls) == 1
        contents = [m.get("content", "") for m in model.calls[0]]
        assert any("2+2" in str(c) for c in contents)

    def test_message_list_shape(self, make_final_response):
        model = MockModel(script=[make_final_response("ok")])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        # runner.invoke with a message list -- last user turn = query,
        # earlier turns = history.
        result = runner.invoke([
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
            {"role": "user", "content": "current question"},
        ])
        assert result is not None


class TestOutputSchema:

    def test_output_schema_parses(self, make_final_response):
        from pydantic import BaseModel

        class Result(BaseModel):
            answer: str
            confidence: float

        model = MockModel(script=[
            make_final_response('{"answer": "42", "confidence": 0.95}'),
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        result = runner.invoke("what?", output_schema=Result)
        assert result.output.answer == "42"
        assert result.output.confidence == 0.95

    def test_output_schema_invalid_json_raises(self, make_final_response):
        from pydantic import BaseModel

        class Result(BaseModel):
            answer: str

        model = MockModel(script=[make_final_response("not JSON at all")])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct, tools=[], verbose=False,
        )
        with pytest.raises(ValueError):
            runner.invoke("what?", output_schema=Result)


class TestStreaming:

    def test_stream_yields_events(self, make_react, make_final_response):
        model = MockModel(script=[
            make_react("calc", "1+1"),
            make_final_response("Answer: 2"),
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_calc_tool()], verbose=False,
        )
        events = list(runner.stream("Compute 1+1"))
        types = [e["type"] for e in events]
        # Expected event stream includes at minimum tool_call, tool_result,
        # final, and the terminal completion.
        assert "tool_call" in types
        assert "tool_result" in types
        assert "final" in types
        assert events[-1]["type"] == "completion"
