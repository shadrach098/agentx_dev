"""A run's returned history never ends with an assistant tool call that has no tool result.

With function calling the model delivers its final answer as a call to the response-format function. The run
used to end without a tool result for that call, so resuming the conversation (``chat_history``) sent a history
that providers reject: "An assistant message with 'tool_calls' must be followed by tool messages".
"""

from agentx_dev import AgentRunner, AgentType
from agentx_dev.Runner.AgentRun import _close_open_tool_calls
from tests.conftest import MockModel


def _final(call_id: str, answer: str) -> dict:
    return {"type": "tool_use", "name": "ReAct", "id": call_id, "input": {
        "Thought": "", "action": "Final_Answer", "action_input": answer}}


def _unanswered(history):
    answered = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    return [call["id"] for m in history if m.get("role") == "assistant"
            for call in m.get("tool_calls") or [] if call["id"] not in answered]


def test_a_function_calling_run_answers_its_final_call():
    model = MockModel(tool_script=[_final("c1", "hello")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                         use_function_calling=True, max_iterations=3)
    result = runner.invoke("hi")
    assert result.content == "hello"
    assert _unanswered(result.history) == []


def test_the_next_turn_sends_a_history_without_open_calls():
    model = MockModel(tool_script=[_final("c1", "first"), _final("c2", "second")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                         use_function_calling=True, max_iterations=3)
    first = runner.invoke("one")
    second = runner.invoke("two", chat_history=first.history)
    assert second.content == "second"
    sent = model.tool_calls_made[1]                    # what the model was sent on the second turn
    assert _unanswered(sent) == []
    assert _unanswered(second.history) == []


def test_a_history_saved_with_an_open_final_call_is_repaired_when_resumed():
    old = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "ReAct", "arguments": "{}"}}]},
    ]
    model = MockModel(tool_script=[_final("c2", "second")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                         use_function_calling=True, max_iterations=3)
    runner.invoke("two", chat_history=old)
    assert _unanswered(model.tool_calls_made[0]) == []


def test_the_helper_leaves_complete_histories_alone():
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function",
                                                            "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    before = list(history)
    _close_open_tool_calls(history)
    assert history == before
    _close_open_tool_calls([])


def test_the_helper_answers_every_open_call_of_the_last_assistant_message():
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "u", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "ok"},
    ]
    _close_open_tool_calls(history)
    assert [m.get("tool_call_id") for m in history if m["role"] == "tool"] == ["a", "b"]


def test_a_native_run_answers_its_respond_call_too():
    def respond(call_id, answer):
        return {"type": "tool_use", "name": "respond", "id": call_id, "input": {"answer": answer}}

    model = MockModel(tool_script=[respond("r1", "first"), respond("r2", "second")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                         bind_tools_natively=True, max_iterations=3)
    first = runner.invoke("one")
    assert first.content == "first" and _unanswered(first.history) == []
    second = runner.invoke("two", chat_history=first.history)
    assert second.content == "second"
    assert _unanswered(model.tool_calls_made[1]) == []
