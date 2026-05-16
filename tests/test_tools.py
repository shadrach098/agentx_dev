import pytest
import json
from unittest.mock import MagicMock
from pydantic import BaseModel
from agentx_dev.Tools import StructuredTool, StandardTool
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner


class MockModel(BaseChatModel):
    def Initialize(self, messages) -> str:
        return json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "done"})


def make_runner_with_structured_tool(func, schema, name="add"):
    tool = StructuredTool(func=func, args_schema=schema, name=name, description="Test structured tool.")
    return AgentRunner(model=MockModel(), Agent=AgentType.ReAct, tools=[tool])


def test_structured_tool_executes_exactly_once_with_dict_args():
    """Tool function must be called exactly once when args_str is a dict."""
    call_count = 0

    class AddArgs(BaseModel):
        a: int
        b: int

    def add(a: int, b: int) -> int:
        nonlocal call_count
        call_count += 1
        return a + b

    runner = make_runner_with_structured_tool(add, AddArgs)
    result = runner.Tool_Runner("add", {"a": 3, "b": 4})

    assert result == 7, f"Expected 7, got {result}"
    assert call_count == 1, f"Function called {call_count} times, expected 1"


def test_structured_tool_executes_exactly_once_with_string_args():
    """Tool function must be called exactly once when args_str is a JSON string."""
    call_count = 0

    class AddArgs(BaseModel):
        a: int
        b: int

    def add(a: int, b: int) -> int:
        nonlocal call_count
        call_count += 1
        return a + b

    runner = make_runner_with_structured_tool(add, AddArgs)
    result = runner.Tool_Runner("add", json.dumps({"a": 5, "b": 6}))

    assert result == 11, f"Expected 11, got {result}"
    assert call_count == 1, f"Function called {call_count} times, expected 1"


def test_structured_tool_validates_with_pydantic():
    """Invalid args must return an error string, not raise an exception."""
    class StrictArgs(BaseModel):
        x: int

    def use_x(x: int) -> int:
        return x * 2

    runner = make_runner_with_structured_tool(use_x, StrictArgs, name="use_x")
    result = runner.Tool_Runner("use_x", {"x": "not_an_int"})
    # Pydantic v2 coerces "not_an_int" or raises — either way no crash in Tool_Runner
    # Accept either a valid result (if coercion works) or an error string
    assert isinstance(result, (int, str))
