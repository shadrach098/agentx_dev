"""fallback_model takes over a run when the model fails for good, and the run carries on where it stopped."""

import logging

import pytest

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.ChatModel import CostBudgetExceeded
from agentx_dev.Runner.Fallback import FallbackFailed
from agentx_dev.Runner.Persistence import BudgetExpired
from tests.conftest import MockModel, _MockToolCallingModel


class KeyRejected(Exception):
    status_code = 401

    def __init__(self):
        super().__init__("Error code: 401 - invalid x-api-key")


class RateLimited(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit reached")


class Scripted(_MockToolCallingModel):
    """A tool-calling mock whose script may hold exceptions: each one is raised by the call that reaches it."""

    def __init__(self, tool_script):
        super().__init__(tool_script=tool_script)
        self.tools_seen = []

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tools_seen.append(tools)
        result = super().call_with_tools(messages, tools, force_tool=force_tool)
        if isinstance(result, BaseException):
            raise result
        return result


class Paid(Scripted):
    model_name = "paid-model"


class Local(Scripted):
    model_name = "local-model"
    prefers_native_tools = True
    is_local = True


def react_call(action, action_input, call_id):
    return {"type": "tool_use", "name": "React_", "id": call_id,
            "input": {"Thought": "do it", "action": action, "action_input": action_input}}


def respond(answer, call_id="r1"):
    return {"type": "tool_use", "name": "respond", "input": {"answer": answer}, "id": call_id}


def counting_tool(counter):
    def run(x):
        counter.append(x)
        return f"counted {x}"
    return StandardTool(func=run, name="count", description="Count something.")


def make(primary, fallback, counter=None, **kw):
    return AgentRunner(model=primary, agent=AgentType.ReAct, tools=[counting_tool(counter if counter is not None else [])],
                       fallback_model=fallback, verbose=False, auto_cache=False, **kw)


SWITCH = {"from": "paid-model", "to": "local-model", "reason": "key rejected"}


def test_switches_and_finishes_on_fallback(caplog):
    runner = make(Paid([KeyRejected()]), Local([respond("finished on the fallback")]))
    with caplog.at_level(logging.WARNING):
        completion = runner.invoke("go")
    assert completion.content == "finished on the fallback"
    assert completion.fallback == SWITCH
    assert runner.fallback_used == SWITCH
    assert any("local-model" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)

    streamed = make(Paid([KeyRejected()]), Local([respond("finished on the fallback")]))
    events = list(streamed.stream("go"))
    fallbacks = [e for e in events if e["type"] == "fallback"]
    assert fallbacks == [{"type": "fallback", **SWITCH}]
    assert events[-1]["completion"].fallback == SWITCH


def test_tool_call_before_failure_is_not_repeated():
    counter = []
    runner = make(Paid([react_call("count", "apples", "c1"), KeyRejected()]),
                  Local([respond("3 apples")]), counter)
    completion = runner.invoke("count the apples")
    assert counter == ["apples"]
    assert completion.content == "3 apples"
    assert [c.name for c in completion.tool_calls] == ["count"]


def assert_valid_for(messages, bound):
    """Review Focus 5, plus strict chat templates: no tool message without its call, no call to a function the
    mode does not bind, and no two non-system messages in a row from the same role."""
    open_ids = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            assert call["function"]["name"] in bound
            open_ids.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in open_ids
    roles = [m["role"] for m in messages if m["role"] != "system"]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles


def test_history_valid_for_native_mode():
    fallback = Local([respond("3 apples")])
    runner = make(Paid([react_call("count", "apples", "c1"), KeyRejected()]), fallback)
    runner.invoke("count the apples")

    messages = fallback.tool_calls_made[0]
    bound = {spec["name"] for spec in fallback.tools_seen[0]}
    assert bound == {"count", "respond"}
    assert_valid_for(messages, bound)
    texts = [str(m.get("content")) for m in messages]
    assert any(t.startswith("[earlier step] called React_ with ") and "apples" in t for t in texts)
    assert any(t.startswith("[earlier result] counted apples") for t in texts)
    assert "Call the 'respond' tool" in messages[0]["content"]       # the native system prompt
    assert runner.bind_tools_natively is True and runner.use_function_calling is False


def test_sticky_next_run_starts_on_fallback():
    primary = Paid([KeyRejected()])
    runner = make(primary, Local([respond("first"), respond("second")]))
    runner.invoke("one")
    events = list(runner.stream("two"))
    assert len(primary.tool_calls_made) == 1                         # the dead provider is not tried again
    assert not [e for e in events if e["type"] == "fallback"]
    completion = events[-1]["completion"]
    assert completion.content == "second"
    assert completion.fallback == SWITCH


def test_reset_fallback_restores_primary():
    primary = Paid([KeyRejected(), react_call("Final_Answer", "back on paid", "c9")])
    runner = make(primary, Local([respond("first")]))
    assert runner.use_function_calling is True and runner.bind_tools_natively is False
    runner.invoke("one")
    runner.reset_fallback()
    assert runner.model is primary
    assert runner.fallback_used is None
    assert runner.use_function_calling is True and runner.bind_tools_natively is False
    completion = runner.invoke("two")
    assert completion.content == "back on paid"
    assert completion.fallback is None


def test_transient_error_does_not_switch():
    fallback = Local([respond("never")])
    runner = make(Paid([RateLimited()]), fallback)
    with pytest.raises(RateLimited):
        runner.invoke("go")
    assert fallback.tool_calls_made == []
    assert runner.fallback_used is None


def test_local_model_never_falls_back():
    class LocalPrimary(Paid):
        is_local = True

    fallback = Local([respond("never")])
    runner = make(LocalPrimary([KeyRejected()]), fallback)
    with pytest.raises(KeyRejected):
        runner.invoke("go")
    assert fallback.tool_calls_made == []


def test_fallback_failing_raises_fallback_failed():
    refused = ConnectionError("connection refused")
    runner = make(Paid([KeyRejected()]), Local([refused]))
    with pytest.raises(FallbackFailed) as raised:
        runner.invoke("go")
    text = str(raised.value)
    assert text == "paid-model failed (key rejected); the fallback local-model failed too: connection refused"
    assert raised.value.__cause__ is refused
    assert raised.value.fallback == SWITCH


@pytest.mark.parametrize("stop", [BudgetExpired("time limit reached"), CostBudgetExceeded(2.0, 1.0)])
def test_framework_stops_never_switch(stop):
    fallback = Local([respond("never")])
    runner = make(Paid([stop]), fallback)
    with pytest.raises(type(stop)):
        runner.invoke("go")
    assert fallback.tool_calls_made == []
    assert runner.fallback_used is None


def test_framework_stop_on_the_fallback_is_not_reported_as_fallback_failed():
    runner = make(Paid([KeyRejected()]), Local([CostBudgetExceeded(2.0, 1.0)]))
    with pytest.raises(CostBudgetExceeded):
        runner.invoke("go")


def test_fallback_is_told_what_happened():
    fallback = Local([respond("3 apples")])
    runner = make(Paid([react_call("count", "apples", "c1"), KeyRejected()]), fallback)
    runner.invoke("count the apples")
    messages = fallback.tool_calls_made[0]
    note = ("[framework] The earlier model (paid-model) stopped: key rejected. You are taking over this task. "
            "What it already did is above; check it and finish the task without redoing steps that are done.")
    last = messages[-1]
    assert last["role"] == "user" and str(last["content"]).endswith(note)
    step = next(i for i, m in enumerate(messages) if str(m.get("content")).startswith("[earlier step]"))
    assert step < len(messages) - 1                              # the note comes after the converted history


def test_switch_over_a_two_turn_fc_history_alternates():
    fallback = Local([respond("done twice")])
    primary = Paid([react_call("count", "a", "c1"), react_call("Final_Answer", "one", "c2"),
                    react_call("count", "b", "c3"), KeyRejected()])
    runner = make(primary, fallback)
    first = runner.invoke("FIRST-QUESTION")
    runner.invoke("SECOND-QUESTION", chat_history=first.history)

    messages = fallback.tool_calls_made[0]
    assert_valid_for(messages, {"count", "respond"})
    text = "\n".join(str(m.get("content")) for m in messages)
    assert "FIRST-QUESTION" in text and "SECOND-QUESTION" in text
    assert "(final answer delivered)" not in text


def test_incoming_fc_history_is_carried_over_for_a_native_runner():
    fc_runner = make(Paid([react_call("count", "a", "c1"), react_call("Final_Answer", "one", "c2")]), None)
    saved = fc_runner.invoke("FIRST-QUESTION").history
    assert any(c["function"]["name"] == "React_" for m in saved for c in m.get("tool_calls") or [])

    local = Local([respond("two")])
    native = make(local, None)
    assert native.bind_tools_natively is True
    assert native.invoke("SECOND-QUESTION", chat_history=saved).content == "two"
    messages = local.tool_calls_made[0]
    assert_valid_for(messages, {"count", "respond"})
    text = "\n".join(str(m.get("content")) for m in messages)
    assert "FIRST-QUESTION" in text and "SECOND-QUESTION" in text
    assert "[earlier step] called React_" in text


def test_valid_incoming_history_is_left_as_it_is():
    fc_runner = make(Paid([react_call("count", "a", "c1"), react_call("Final_Answer", "one", "c2")]), None)
    saved = fc_runner.invoke("FIRST-QUESTION").history
    again = Paid([react_call("Final_Answer", "two", "c3")])
    make(again, None).invoke("SECOND-QUESTION", chat_history=saved)
    assert again.tool_calls_made[0][1:-1] == saved[1:]          # everything but the system prompt and new task


def test_helpers_built_after_a_switch_use_the_fallback():
    from agentx_dev.SubAgents import AgentSpec, SpawnConfig

    primary, fallback = Paid([KeyRejected()]), Local([respond("done")])
    runner = make(primary, fallback, delegation=SpawnConfig(enabled=True, capabilities={"web"}))
    runner.invoke("go")
    spec = AgentSpec(name="helper", instructions="Help.")
    assert runner._delegation._make_runner(spec, [], [], False).model is fallback
    runner.reset_fallback()
    assert runner._delegation._make_runner(spec, [], [], False).model is primary


def test_text_mode_primary_switches_too(make_react):
    def no_credit(messages):
        raise RuntimeError("Your credit balance is too low to access the Anthropic API.")

    counter = []
    calls = iter([make_react("count", "pears")])

    def script(messages):
        step = next(calls, None)
        return step if step is not None else no_credit(messages)

    primary = MockModel(script=script)
    primary.model_name = "paid-text"
    fallback = Local([respond("2 pears")])
    runner = make(primary, fallback, counter)
    assert runner.use_function_calling is False
    completion = runner.invoke("count the pears")
    assert counter == ["pears"]
    assert completion.content == "2 pears"
    assert completion.fallback == {"from": "paid-text", "to": "local-model", "reason": "out of credit"}
    assert not any(m["role"] == "tool" for m in fallback.tool_calls_made[0])


def test_persistent_run_switches_and_marks_completion():
    from agentx_dev import Persistence

    counter = []
    runner = make(Paid([react_call("count", "plums", "c1"), KeyRejected()]),
                  Local([respond("4 plums")]), counter, persistence=Persistence(max_minutes=5))
    completion = runner.invoke("count the plums")
    assert counter == ["plums"]
    assert completion.content == "4 plums"
    assert completion.fallback == SWITCH


def test_carry_over_keeps_valid_calls_and_their_results():
    from agentx_dev.Runner.AgentRun import _carry_over_history

    def call(call_id, name):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}

    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [call("a", "count"), call("b", "React_")]},
        {"role": "tool", "tool_call_id": "b", "name": "React_", "content": "gone"},
        {"role": "tool", "tool_call_id": "a", "name": "count", "content": "kept"},
        {"role": "user", "content": "next"},
    ]
    assert _carry_over_history(history, {"count", "respond"}, keep=5) == 4
    assert history == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "[earlier step] called React_ with {}", "tool_calls": [call("a", "count")]},
        {"role": "tool", "tool_call_id": "a", "name": "count", "content": "kept"},
        {"role": "user", "content": "[earlier result] gone\n\nnext"},
    ]


def test_streamed_text_call_switches_too():
    def no_credit(messages):
        raise RuntimeError("insufficient_quota: You exceeded your current quota")

    primary = MockModel(script=no_credit)
    primary.model_name = "paid-text"
    runner = make(primary, Local([respond("streamed on the fallback")]))
    events = list(runner.stream("go", stream_tokens=True))
    assert [e for e in events if e["type"] == "fallback"] == [
        {"type": "fallback", "from": "paid-text", "to": "local-model", "reason": "out of credit"}]
    assert events[-1]["completion"].content == "streamed on the fallback"
