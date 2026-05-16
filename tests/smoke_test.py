"""End-to-end smoke tests — no real API calls, uses mock models."""
import json
import pytest
from pydantic import BaseModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool, StructuredTool


class MockModel(BaseChatModel):
    def __init__(self, responses):
        self._responses = iter(responses)

    def Initialize(self, messages) -> str:
        return next(self._responses)


def test_tool_call_then_final_answer():
    def greet(name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"

    tool = StandardTool(func=greet, name="greet", description="Greet a person by name.")
    responses = [
        json.dumps({"Thought": "I'll greet them.", "action": "greet", "action_input": "Alice"}),
        json.dumps({"Thought": "Done.", "action": "Final_Answer", "action_input": "Hello, Alice!"}),
    ]
    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])
    result = runner.Initialize("Say hi to Alice")
    assert result.content == "Hello, Alice!"
    assert result.history is not None
    assert len(result.history) > 0


def test_structured_tool_call():
    class MulArgs(BaseModel):
        a: int
        b: int

    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    tool = StructuredTool(func=multiply, args_schema=MulArgs, name="multiply", description="Multiply two numbers.")
    responses = [
        json.dumps({"Thought": "multiply", "action": "multiply", "action_input": {"a": 6, "b": 7}}),
        json.dumps({"Thought": "done", "action": "Final_Answer", "action_input": "42"}),
    ]
    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])
    result = runner.Initialize("What is 6 times 7?")
    assert result.content == "42"
    assert result.history is not None


def test_runner_reusable_across_calls():
    """Same runner instance must work correctly on multiple calls."""
    def dummy(x: str) -> str:
        """dummy tool"""
        return x

    tool = StandardTool(func=dummy, name="dummy", description="A dummy tool.")
    responses = [
        json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "first"}),
        json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "second"}),
    ]

    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])

    r1 = runner.Initialize("call one")
    r2 = runner.Initialize("call two")

    assert r1.content == "first"
    assert r2.content == "second"

    # System prompt of second call must NOT contain "call one"
    sys_msg = next(m for m in r2.history if m["role"] == "system")
    assert "call one" not in sys_msg["content"]
    assert "call two" in sys_msg["content"]


def test_tool_description_not_mutated():
    """Tool description must not be modified after runner creation."""
    class AddArgs(BaseModel):
        a: int
        b: int

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    tool = StructuredTool(func=add, args_schema=AddArgs, name="add", description="Add two numbers.")
    original_desc = tool.description

    runner = AgentRunner(
        model=MockModel([json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "done"})]),
        Agent=AgentType.ReAct,
        tools=[tool]
    )

    assert tool.description == original_desc, (
        f"Tool description was mutated! Expected: '{original_desc}', Got: '{tool.description}'"
    )
    # But the prompt block should include param info
    assert "required params" in runner._tool_prompt_block


def test_claude_is_importable():
    """Claude must be importable from the top-level package."""
    from agentx_dev import Claude
    from agentx_dev.ChatModel import BaseChatModel
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    claude = Claude(model="claude-haiku-4-5-20251001")
    assert isinstance(claude, BaseChatModel)
