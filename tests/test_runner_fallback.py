"""fallback_model takes over a run when the model fails for good, and the run carries on where it stopped."""

import logging

import pytest

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Runner.Fallback import FallbackFailed
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


def test_history_valid_for_native_mode():
    fallback = Local([respond("3 apples")])
    runner = make(Paid([react_call("count", "apples", "c1"), KeyRejected()]), fallback)
    runner.invoke("count the apples")

    messages = fallback.tool_calls_made[0]
    bound = {spec["name"] for spec in fallback.tools_seen[0]}
    assert bound == {"count", "respond"}
    open_ids = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            assert call["function"]["name"] in bound
            open_ids.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in open_ids
    texts = [str(m.get("content")) for m in messages]
    assert any(t.startswith("[earlier step] called React_ with ") and "apples" in t for t in texts)
    assert any(t == "[earlier result] counted apples" for t in texts)
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
    runner = make(Paid([KeyRejected()]), Local([ConnectionError("connection refused")]))
    with pytest.raises(FallbackFailed) as raised:
        runner.invoke("go")
    text = str(raised.value)
    assert text == "paid-model failed (key rejected); the fallback local-model failed too: connection refused"


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
    _carry_over_history(history, {"count", "respond"})
    assert history == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "[earlier step] called React_ with {}", "tool_calls": [call("a", "count")]},
        {"role": "tool", "tool_call_id": "a", "name": "count", "content": "kept"},
        {"role": "user", "content": "[earlier result] gone"},
        {"role": "user", "content": "next"},
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
