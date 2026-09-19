"""GPT falls back to the Responses API when chat completions can't do tools.

Reproduces a user trace on ``gpt-6-astra``:

1. chat completions rejects ``reasoning_effort='none'`` (supported:
   low / medium / high / xhigh)  -> adapted to 'low';
2. chat completions then rejects function tools with ANY reasoning effort:
   "Function tools with reasoning_effort are not supported for gpt-6-astra
   in /v1/chat/completions. To use function tools, use /v1/responses or set
   reasoning_effort to 'none'." -- and 'none' is exactly what (1) rejected.

The only way to call tools on that model is /v1/responses.
"""

import json
import types

import httpx
import openai
import pytest

from agentx_dev import AgentRunner, AgentType, GPT, Media, StandardTool

MODEL = "gpt-6-astra"
EFFORT_MSG = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
              "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")
TOOLS_MSG = (f"Function tools with reasoning_effort are not supported for {MODEL} in "
             "/v1/chat/completions. To use function tools, use /v1/responses or set "
             "reasoning_effort to 'none'.")


def _400(message, code, param, path="chat/completions"):
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    resp = httpx.Response(400, request=httpx.Request("POST", f"https://api.openai.com/v1/{path}"),
                          json={"error": body})
    return openai.BadRequestError(message=f"Error code: 400 - {body}", response=resp, body=body)


def _set(v):
    return v is not None and v is not openai.NOT_GIVEN


class FakeOpenAI:
    """chat.completions behaves like gpt-6-astra; responses works."""

    def __init__(self, responses_output=None):
        self.chat_calls, self.responses_calls = [], []
        self.responses_output = responses_output
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))
        self.responses = types.SimpleNamespace(create=self._responses)

    def _chat(self, **kw):
        self.chat_calls.append(kw)
        effort = kw.get("reasoning_effort")
        if effort == "none":
            raise _400(EFFORT_MSG, "unsupported_value", "reasoning_effort")
        if _set(kw.get("tools")) and _set(effort):
            raise _400(TOOLS_MSG, None, "reasoning_effort")
        msg = types.SimpleNamespace(content="plain text answer", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    def _responses(self, **kw):
        self.responses_calls.append(kw)
        if (kw.get("reasoning") or {}).get("effort") == "none":
            raise _400(EFFORT_MSG.replace("'reasoning_effort'", "'reasoning.effort'"),
                       "unsupported_value", "reasoning.effort", path="responses")
        output = self.responses_output or [types.SimpleNamespace(
            type="function_call", name="weather", arguments='{"input": "Accra"}',
            call_id="call_abc", id="fc_1")]
        return types.SimpleNamespace(output=output,
                                     usage=types.SimpleNamespace(input_tokens=3, output_tokens=2))


def make_gpt(**kw):
    g = GPT(api_key="x", model=MODEL, reasoning_effort="none", **kw)
    fake = FakeOpenAI()
    g.client = fake
    return g, fake


TOOLS = [{"name": "weather", "description": "Weather.",
          "parameters": {"type": "object", "properties": {"input": {"type": "string"}}}}]
MSGS = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "weather in Accra?"}]


class TestAutomaticSwitch:
    def test_user_trace_now_succeeds(self):
        g, fake = make_gpt()
        out = g.call_with_tools(MSGS, TOOLS)
        assert out["type"] == "tool_use"
        assert out["name"] == "weather" and out["input"] == {"input": "Accra"}
        assert out["id"] == "call_abc", "must return call_id, not the fc_ item id"
        sent = fake.responses_calls[-1]
        assert sent["reasoning"] == {"effort": "low"}, "the learned 'none'->'low' fix must carry over"

    def test_switch_is_remembered(self):
        g, fake = make_gpt()
        g.call_with_tools(MSGS, TOOLS)
        chat_before = len(fake.chat_calls)
        g.call_with_tools(MSGS, TOOLS)
        assert len(fake.chat_calls) == chat_before, "second call retried chat completions"
        assert len(fake.responses_calls) == 2

    def test_text_calls_stay_on_chat_completions(self):
        g, fake = make_gpt()
        g.call_with_tools(MSGS, TOOLS)
        assert g.invoke("hi") == "plain text answer"
        assert fake.chat_calls[-1].get("reasoning_effort") == "low"

    def test_forced_tool_choice_shape(self):
        g, fake = make_gpt()
        g.call_with_tools(MSGS, TOOLS, force_tool="weather")
        assert fake.responses_calls[-1]["tool_choice"] == {"type": "function", "name": "weather"}

    def test_opt_out_surfaces_the_provider_error(self):
        g, fake = make_gpt(use_responses_api=False)
        with pytest.raises(openai.BadRequestError, match="/v1/responses"):
            g.call_with_tools(MSGS, TOOLS)
        assert fake.responses_calls == []

    def test_unrelated_400_does_not_switch(self):
        g, fake = make_gpt()

        def overflow(**kw):
            raise _400("This model's maximum context length is 128000 tokens.",
                       "context_length_exceeded", "messages")

        fake.chat.completions.create = overflow
        with pytest.raises(openai.BadRequestError):
            g.call_with_tools(MSGS, TOOLS)
        assert fake.responses_calls == []


class TestWireFormat:
    def test_request_shape(self):
        g, fake = make_gpt(max_tokens=900, seed=7)
        g.call_with_tools(MSGS, TOOLS)
        sent = fake.responses_calls[-1]
        assert sent["tools"] == [{"type": "function", "name": "weather", "description": "Weather.",
                                  "parameters": TOOLS[0]["parameters"], "strict": False}]
        assert sent["max_output_tokens"] == 900
        assert sent["store"] is False, "Responses stores by default; the framework must not"
        assert "seed" not in sent and "messages" not in sent
        assert sent["input"][0] == {"role": "system", "content": "be brief"}

    def test_tool_history_round_trips(self):
        g, fake = make_gpt()
        history = MSGS + [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_abc", "type": "function",
                "function": {"name": "weather", "arguments": '{"input": "Accra"}'}}]},
            {"role": "tool", "tool_call_id": "call_abc", "name": "weather", "content": "sunny"},
        ]
        g.call_with_tools(history, TOOLS)
        items = fake.responses_calls[-1]["input"]
        call = next(i for i in items if i.get("type") == "function_call")
        out = next(i for i in items if i.get("type") == "function_call_output")
        assert call == {"type": "function_call", "call_id": "call_abc", "name": "weather",
                        "arguments": '{"input": "Accra"}'}
        assert "id" not in call, "an fc_ id without its reasoning item is rejected"
        assert out == {"type": "function_call_output", "call_id": "call_abc", "output": "sunny"}

    def test_media_uses_responses_parts(self):
        g, fake = make_gpt()
        g.call_with_tools([{"role": "user", "content": [
            "look",
            Media.image(b"img", media_type="image/png", detail="low"),
            Media.document("https://x.test/r.pdf"),
            Media.audio(b"RIFF", media_type="audio/wav"),
        ]}], TOOLS)
        parts = fake.responses_calls[-1]["input"][0]["content"]
        assert [p["type"] for p in parts] == ["input_text", "input_image", "input_file", "input_audio"]
        assert parts[1]["image_url"].startswith("data:image/png;base64,") and parts[1]["detail"] == "low"
        assert parts[2] == {"type": "input_file", "file_url": "https://x.test/r.pdf"}

    def test_text_output_parsed(self):
        g, fake = make_gpt()
        fake.responses_output = [
            types.SimpleNamespace(type="reasoning", summary=[]),
            types.SimpleNamespace(type="message", content=[
                types.SimpleNamespace(type="output_text", text="It is sunny.")]),
        ]
        assert g.call_with_tools(MSGS, TOOLS) == {"type": "text", "text": "It is sunny."}

    def test_usage_recorded(self):
        g, _ = make_gpt()
        g.call_with_tools(MSGS, TOOLS)
        assert g.usage.last_input_tokens == 3 and g.usage.last_output_tokens == 2

    def test_always_mode_routes_text_calls_too(self):
        g, fake = make_gpt(use_responses_api=True)
        fake.responses_output = [types.SimpleNamespace(type="message", content=[
            types.SimpleNamespace(type="output_text", text="via responses")])]
        assert g.invoke("hi") == "via responses"
        assert fake.chat_calls == []


class TestAgentRunEndToEnd:
    def test_function_calling_agent_completes_on_the_model(self):
        """The exact failure: an AgentRunner (FC mode, auto-detected) on
        gpt-6-astra with reasoning_effort='none'."""
        g = GPT(api_key="x", model=MODEL, reasoning_effort="none")
        fake = FakeOpenAI()
        steps = iter([
            [types.SimpleNamespace(type="function_call", name="React_", call_id="call_1", id="fc_1",
                                   arguments=json.dumps({"Thought": "t", "action": "weather",
                                                         "action_input": "Accra"}))],
            [types.SimpleNamespace(type="function_call", name="React_", call_id="call_2", id="fc_2",
                                   arguments=json.dumps({"Thought": "t", "action": "Final_Answer",
                                                         "action_input": "It is sunny in Accra."}))],
        ])
        original = fake._responses

        def scripted(**kw):
            fake.responses_output = next(steps)
            return original(**kw)

        fake.responses.create = scripted
        g.client = fake
        tool = StandardTool(func=lambda q: "sunny", name="weather", description="Weather.")
        runner = AgentRunner(model=g, agent=AgentType.ReAct, tools=[tool], verbose=False)
        assert runner.use_function_calling is True
        result = runner.invoke("Weather in Accra?")
        assert result.content == "It is sunny in Accra."
        assert [t.name for t in result.tool_calls] == ["weather"]
        second = fake.responses_calls[-1]["input"]
        assert any(i.get("type") == "function_call_output" and i.get("call_id") == "call_1"
                   for i in second), "tool result must be fed back by call_id"
