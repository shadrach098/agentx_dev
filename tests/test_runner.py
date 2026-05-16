import pytest
import json
from unittest.mock import MagicMock
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool


class MockModel(BaseChatModel):
    def __init__(self, responses):
        self._responses = iter(responses)

    def Initialize(self, messages) -> str:
        return next(self._responses)


def make_runner(responses):
    def dummy_tool(input: str) -> str:
        """A dummy tool for testing."""
        return f"tool_result:{input}"

    tool = StandardTool(func=dummy_tool, name="dummy", description="A dummy tool.")
    return AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])


def test_second_call_does_not_use_first_querys_system_prompt():
    final_answer_1 = json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "answer1"})
    final_answer_2 = json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "answer2"})

    runner = make_runner([final_answer_1, final_answer_2])

    runner.Initialize("first question")
    runner.Initialize("second question")

    second_call_messages = runner.model.Initialize.call_args_list[1][1]["messages"] if hasattr(runner.model.Initialize, 'call_args_list') else None

    # Re-run with a spy model to capture messages
    class SpyModel(BaseChatModel):
        def __init__(self, responses):
            self._responses = iter(responses)
            self.all_message_batches = []

        def Initialize(self, messages) -> str:
            self.all_message_batches.append(list(messages))
            return next(self._responses)

    def dummy_tool(input: str) -> str:
        """A dummy tool for testing."""
        return f"result"

    tool = StandardTool(func=dummy_tool, name="dummy", description="A dummy tool.")
    spy = SpyModel([final_answer_1, final_answer_2])
    runner2 = AgentRunner(model=spy, Agent=AgentType.ReAct, tools=[tool])

    runner2.Initialize("first question")
    runner2.Initialize("second question")

    second_call_system = next(m["content"] for m in spy.all_message_batches[1] if m["role"] == "system")
    assert "first question" not in second_call_system, "System prompt for second call must NOT contain first query."
    assert "second question" in second_call_system


def test_chat_history_not_duplicated_across_calls():
    final_answer = json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "done"})

    class SpyModel(BaseChatModel):
        def __init__(self, responses):
            self._responses = iter(responses)
            self.all_message_batches = []

        def Initialize(self, messages) -> str:
            self.all_message_batches.append(list(messages))
            return next(self._responses)

    def dummy_tool(input: str) -> str:
        """A dummy tool."""
        return "r"

    tool = StandardTool(func=dummy_tool, name="dummy", description="A dummy tool.")
    spy = SpyModel([final_answer, final_answer])
    runner = AgentRunner(model=spy, Agent=AgentType.ReAct, tools=[tool])

    history = [{"role": "assistant", "content": "hello"}]
    runner.Initialize("q1", ChatHistory=history)
    runner.Initialize("q2", ChatHistory=history)

    hello_count = sum(1 for m in spy.all_message_batches[1] if m.get("content") == "hello")
    assert hello_count == 1, f"Expected 1 copy of history message in second call, got {hello_count}"


def test_history_returned_in_completion():
    final_answer = json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "done"})
    runner = make_runner([final_answer])
    result = runner.Initialize("test")
    assert result.history is not None
    assert isinstance(result.history, list)
    assert len(result.history) > 0
