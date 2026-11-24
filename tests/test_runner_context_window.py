"""The runner keeps each request inside the model's context window (a local server silently drops the start of a
longer request, which is where the instructions are)."""

import json
import math

import pytest

import agentx_dev
from agentx_dev import AgentRunner, AgentType, ContextWindowTooSmall, Persistence, StandardTool
from agentx_dev.Runner.Trim import request_chars
from tests.conftest import MockModel, _MockToolCallingModel, make_final, make_react_response


class KeyRejected(Exception):
    status_code = 401

    def __init__(self):
        super().__init__("Error code: 401 - invalid x-api-key")


class Scripted(_MockToolCallingModel):
    """A tool-calling mock: ``tool_script`` answers ``call_with_tools`` (an exception in it is raised),
    ``script`` answers ``Initialize`` (the summary call). Records every request."""

    model_name = "local-model"
    prefers_native_tools = True
    is_local = True

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, tool_script, script=None, context_window=None):
        super().__init__(script=list(script or []), tool_script=tool_script)
        self.context_window = context_window
        self.tools_seen = []

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tools_seen.append(tools)
        result = super().call_with_tools(messages, tools, force_tool=force_tool)
        if isinstance(result, BaseException):
            raise result
        return result


class Paid(Scripted):
    model_name = "paid-model"
    prefers_native_tools = False
    is_local = False


def call(name, arg, call_id):
    return {"type": "tool_use", "name": name, "input": {"x": arg}, "id": call_id}


def respond(answer="done"):
    return {"type": "tool_use", "name": "respond", "input": {"answer": answer}, "id": "r1"}


def react_call(arg, call_id):
    return {"type": "tool_use", "name": "React_", "id": call_id,
            "input": {"Thought": "read it", "action": "read", "action_input": arg}}


def reader(size):
    return StandardTool(func=lambda x: f"{x}:" + "r" * (size - len(x) - 1), name="read",
                        description="Read a part.")


def make(model, size=3000, **kw):
    kw.setdefault("max_iterations", 20)
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[reader(size)], verbose=False,
                       auto_cache=False, **kw)


def sent_chars(messages):
    """What a request carries, measured the simple way: message text and call arguments."""
    total = 0
    for message in messages:
        if isinstance(message.get("content"), str):
            total += len(message["content"])
        for tool_call in message.get("tool_calls") or []:
            total += len(tool_call["function"]["arguments"])
    return total


def five_reads():
    return [call("read", str(i), f"c{i}") for i in range(5)] + [respond()]


def test_no_window_no_change():
    model = Scripted(five_reads())
    events = list(make(model).stream("read five parts"))
    assert not [e for e in events if e["type"] == "context_trimmed"]
    last = model.tool_calls_made[-1]
    results = [m["content"] for m in last if m["role"] == "tool"]
    assert len(results) == 5 and all(len(r) == 3000 for r in results)        # nothing stubbed
    history = events[-1]["completion"].history
    assert last == history[:len(last)]                                       # the history, as it is


def test_old_tool_results_stubbed_to_fit():
    model = Scripted(five_reads())
    events = list(make(model, context_window=2000).stream("read five parts"))
    budget = 0.85 * 2000 * 4
    for request in model.tool_calls_made:
        assert sent_chars(request) <= budget
        tool_messages = [m for m in request if m["role"] == "tool"]
        if tool_messages:
            assert len(tool_messages[-1]["content"]) == 3000                 # the newest result, whole
    last = [m["content"] for m in model.tool_calls_made[-1] if m["role"] == "tool"]
    assert any(len(r) < 3000 for r in last[:-1])                             # older ones were stubbed
    assert [e for e in events if e["type"] == "context_trimmed"] == [
        {"type": "context_trimmed", "window": 2000, "how": "results"}]
    history = events[-1]["completion"].history
    kept = [m["content"] for m in history if m["role"] == "tool" and m.get("name") == "read"]
    assert len(kept) == 5 and all(len(r) == 3000 for r in kept)                # the history is not changed


def test_summary_when_stubs_not_enough():
    # Results under 800 characters are never stubbed, so only a summary makes room.
    script = [call("read", str(i), f"c{i}") for i in range(12)] + [respond()]
    model = Scripted(script, script=["notes: read parts 0 to 11"] * 5, context_window=2000)
    events = list(make(model, size=700).stream("read twelve parts"))
    trimmed = [e for e in events if e["type"] == "context_trimmed"]
    assert trimmed == [{"type": "context_trimmed", "window": 2000, "how": "summary"}]
    assert model.calls, "the summary call was made"
    for summary_input in model.calls:
        assert sum(len(m["content"]) for m in summary_input) <= 4000          # half the window
    for request in model.tool_calls_made:
        assert sent_chars(request) <= 0.85 * 2000 * 4
    final = model.tool_calls_made[-1]
    assert "notes: read parts 0 to 11" in final[1]["content"]                  # the task message carries the notes
    assert events[-1]["completion"].content == "done"


def test_persistent_run_keeps_its_task_message_and_ledger():
    script = [call("read", str(i), f"c{i}") for i in range(12)] + [respond()]
    model = Scripted(script, script=["notes: read parts 0 to 11"] * 5, context_window=2000)
    completion = make(model, size=700, persistence=Persistence()).invoke("read twelve parts")
    assert completion.content == "done"
    task = model.tool_calls_made[-1][1]
    assert task["role"] == "user" and task["content"].startswith("read twelve parts")
    assert "Failed attempts so far" in task["content"]                       # the notes carry the ledger
    assert all(sent_chars(r) <= 0.85 * 2000 * 4 for r in model.tool_calls_made)


def test_earlier_conversation_summarised_when_the_turn_alone_fits():
    earlier = []
    for i in range(4):
        earlier += [{"role": "user", "content": f"question {i} " + "q" * 1500},
                    {"role": "assistant", "content": f"answer {i} " + "a" * 1500}]
    model = Scripted([respond()], script=["notes: four questions answered"], context_window=2000)
    completion = make(model).invoke("one more question", chat_history=earlier)
    assert completion.content == "done"
    request = model.tool_calls_made[0]
    assert sent_chars(request) <= 0.85 * 2000 * 4
    assert request[-1]["content"] == "one more question"                       # the newest message, whole
    roles = [m["role"] for m in request[1:]]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles


def test_too_small_raises():
    model = Scripted([respond()], context_window=1000)
    runner = make(model, system_addendum="s" * 10_000)
    with pytest.raises(ContextWindowTooSmall) as raised:
        runner.invoke("hello")
    assert raised.value.window == 1000
    assert raised.value.needed > 1000
    assert model.tool_calls_made == []                                         # the request was not sent


class Counting(Scripted):
    """Reports ``prompt_tokens`` as half the characters it was sent: two characters per token."""

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tools_seen.append(tools)
        self.tool_calls_made.append(list(messages))
        chars = request_chars(messages) + len(json.dumps(tools, default=str))
        tokens = math.ceil(chars / getattr(self, "per_token", 2))
        cached = int(tokens * getattr(self, "cached", 0.0))          # reported apart, as Anthropic does
        self._record_usage_counts(input_tokens=tokens - cached, output_tokens=5, cache_read_tokens=cached)
        return self._tool_script.pop(0)


def test_observed_ratio_used():
    window = 2000
    model = Counting(five_reads(), context_window=window)
    events = list(make(model, size=1500).stream("read five parts"))
    assert events[-1]["completion"].content == "done"
    for request in model.tool_calls_made[1:]:                                  # after the first reply's usage
        assert sent_chars(request) <= 0.85 * window * 2

    # The same run with the estimate of four characters per token sends more than that.
    estimated = Scripted(five_reads(), context_window=window)
    list(make(estimated, size=1500).stream("read five parts"))
    assert max(sent_chars(r) for r in estimated.tool_calls_made) > 0.85 * window * 2


@pytest.mark.parametrize("per_token, cached", [(4, 0.5), (6, 0.0)])
def test_a_reply_never_loosens_the_budget(per_token, cached):
    # Half the tokens reported as cache reads (input_tokens alone shows 8 characters a token), or a server that
    # reports 6: the budget stays at 4 characters a token.
    model = Counting(five_reads(), context_window=2000)
    model.per_token, model.cached = per_token, cached
    assert make(model).invoke("read five parts").content == "done"
    assert all(sent_chars(r) <= 0.85 * 2000 * 4 for r in model.tool_calls_made)


class CountingOpenAIStyle(Scripted):
    """Reports usage as OpenAI and Ollama do: ``prompt_tokens`` is the whole prompt, cached tokens included, and the
    cached part is given again on its own. Four characters a token, almost all of it cached (a warm prefix cache)."""
    input_tokens_include_cached = True

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tool_calls_made.append(list(messages))
        tokens = math.ceil((request_chars(messages) + len(json.dumps(tools, default=str))) / 4)
        self._record_usage_counts(input_tokens=tokens, output_tokens=5, cache_read_tokens=int(tokens * 0.95))
        return self._tool_script.pop(0)


def test_cached_tokens_inside_the_prompt_count_are_not_counted_twice():
    # Ollama on the user's laptop: 14,640 prompt tokens, 14,397 of them cached. Adding the cached ones again made the
    # runner believe a 40,960-token window held half of what it does, and a skill read "did not fit".
    model = CountingOpenAIStyle(five_reads(), context_window=2000)
    runner = make(model)
    assert runner.invoke("read five parts").content == "done"
    assert runner._chars_per_token[id(model)] >= 3.9


def test_one_oversized_result_is_cut_to_fit():
    model = Scripted([call("read", "big", "c0"), respond()], script=["notes"] * 3, context_window=4000)
    completion = make(model, size=60_000).invoke("read the big file")
    assert completion.content == "done"
    request = model.tool_calls_made[-1]
    assert sent_chars(request) <= 0.85 * 4000 * 4
    result = [m for m in request if m["role"] == "tool"][-1]["content"]
    assert result.startswith("big:rrr") and result.endswith("rrr")             # its start and its end
    assert "characters cut to fit the model's window" in result
    kept = [m for m in completion.history if m["role"] == "tool" and m.get("name") == "read"]
    assert len(kept[0]["content"]) == 60_000                                    # the history keeps it whole


def test_one_oversized_observation_is_cut_in_text_mode():
    model = MockModel(script=[make_react_response("read", "big"), make_final("done")])
    completion = make(model, size=60_000, context_window=4000).invoke("read the big file")
    assert completion.content == "done"
    request = model.calls[-1]
    assert sent_chars(request) <= 0.85 * 4000 * 4
    assert "characters cut to fit the model's window" in request[-1]["content"]


def test_persistence_summary_fits_the_window():
    # Persistence compacts on its own schedule (here after ~6,000 tokens); its summary call fits the window too.
    script = [call("read", str(i), f"c{i}") for i in range(24)] + [respond()]
    model = Scripted(script, script=["notes"] * 10, context_window=8192)
    runner = make(model, size=1400, max_iterations=40, persistence=Persistence(compact_at_tokens=6000))
    events = list(runner.stream("read many parts"))
    assert events[-1]["completion"].content == "done"
    assert [e for e in events if e["type"] == "compact"], "persistence compacted"
    assert model.calls
    assert all(sum(len(m["content"]) for m in summary) <= 8192 * 4 // 2 for summary in model.calls)


def test_explicit_window_is_the_primarys_not_the_fallbacks():
    primary = Paid([react_call(str(i), f"c{i}") for i in range(5)] + [KeyRejected()])
    fallback = Scripted([respond("finished")], script=["notes"] * 3, context_window=2000)
    runner = make(primary, fallback_model=fallback, context_window=200_000)
    assert runner.invoke("read five parts").content == "finished"
    assert all(sent_chars(r) <= 0.85 * 2000 * 4 for r in fallback.tool_calls_made)


def test_budget_follows_fallback():
    primary = Paid([react_call(str(i), f"c{i}") for i in range(5)] + [KeyRejected()])
    fallback = Scripted([respond("finished")], script=["notes: five parts read"], context_window=2000)
    runner = make(primary, fallback_model=fallback)
    events = list(runner.stream("read five parts"))
    assert all(sent_chars(r) > 0 for r in primary.tool_calls_made)
    assert sent_chars(primary.tool_calls_made[-1]) > 0.85 * 2000 * 4         # the primary had no limit
    retried = fallback.tool_calls_made[0]
    assert sent_chars(retried) <= 0.85 * 2000 * 4
    assert [e["type"] for e in events if e["type"] in ("fallback", "context_trimmed")] == [
        "fallback", "context_trimmed"]
    assert events[-1]["completion"].content == "finished"


def test_text_mode_requests_fit_too():
    script = [make_react_response("read", str(i)) for i in range(5)] + [make_final("done")]
    model = MockModel(script=script)
    completion = make(model, size=1500, context_window=3000).invoke("read five parts")
    assert completion.content == "done"
    assert len(model.calls) == 6
    for request in model.calls:
        assert sent_chars(request) <= 0.85 * 3000 * 4
    assert any(len(m["content"]) < 1000 for m in model.calls[-1] if m["content"].startswith("Observation: "))


def test_runner_window_overrides_the_models():
    model = Scripted(five_reads(), context_window=1_000_000)
    make(model, context_window=2000).invoke("read five parts")
    assert all(sent_chars(r) <= 0.85 * 2000 * 4 for r in model.tool_calls_made)


def test_exported():
    assert "ContextWindowTooSmall" in agentx_dev.__all__
    error = ContextWindowTooSmall(window=8192, needed=9000)
    assert (error.window, error.needed) == (8192, 9000)


# -- fixed_request_chars: what every request carries before the conversation ---------------------------------------

@pytest.mark.parametrize("native", [True, False])
def test_fixed_request_chars_is_what_the_first_request_needs_before_the_task(native):
    model = Scripted([respond()]) if native else Paid([react_call("a", "c1")])
    runner = make(model, system_addendum="RULES " * 500)
    fixed = runner.fixed_request_chars()
    assert fixed > 3000
    task = "go"
    tight = math.floor((fixed + len(task)) / 4 / 0.85) - 20          # a window just too small for the prompt and tools
    model.context_window = tight
    with pytest.raises(ContextWindowTooSmall):
        runner.invoke(task)
    model.context_window = tight + 200
    model._tool_script[:] = [respond()] if native else [react_call("a", "c1"), {"type": "tool_use", "name": "React_",
                             "id": "c2", "input": {"Thought": "t", "action": "Final_Answer", "action_input": "ok"}}]
    runner.invoke(task)


def test_fixed_request_chars_for_another_model_uses_its_binding():
    native = Scripted([respond()])
    runner = make(Paid([]), system_addendum="RULES")
    assert runner.fixed_request_chars(native) != runner.fixed_request_chars()
    assert runner.model is not native and not runner.bind_tools_natively       # nothing about the runner changed
