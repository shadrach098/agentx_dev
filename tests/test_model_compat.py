"""GPT / Claude adapt to whatever request parameters a model generation accepts.

Every test drives the real ``GPT`` / ``Claude`` class against a fake SDK
client that rejects requests with the exact 400 bodies the providers send.
"""

import types

import httpx
import openai
import anthropic
import pytest

from agentx_dev import GPT, Claude


def openai_400(message, code, param):
    body = {"message": message, "type": "invalid_request_error",
            "param": param, "code": code}
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
                          json={"error": body})
    return openai.BadRequestError(message=f"Error code: 400 - {body}", response=resp, body=body)


def anthropic_400(message):
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                          json={"type": "error", "error": {"type": "invalid_request_error", "message": message}})
    return anthropic.BadRequestError(message=message, response=resp, body=resp.json())


def sent(v):
    return v is not None and v is not openai.NOT_GIVEN


class _FakeCompletions:
    def __init__(self, rule):
        self.rule, self.calls = rule, []

    def create(self, **kw):
        self.calls.append(kw)
        err = self.rule(kw)
        if err:
            raise err
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def gpt(rule, **kw):
    g = GPT(api_key="x", **kw)
    fake = _FakeCompletions(rule)
    g.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
    return g, fake


def effort_rule(rejected, supported):
    listed = ", ".join(f"'{v}'" for v in supported)

    def rule(kw):
        if kw.get("reasoning_effort") == rejected:
            return openai_400(
                f"Unsupported value: 'reasoning_effort' does not support '{rejected}' "
                f"with this model. Supported values are: {listed}.",
                "unsupported_value", "reasoning_effort")
    return rule


class TestReasoningEffort:
    def test_none_moves_to_nearest_supported(self):
        # The exact error from a user trace.
        g, fake = gpt(effort_rule("none", ["low", "medium", "high", "xhigh"]),
                      model="gpt-5.4", reasoning_effort="none")
        assert g.invoke("hi") == "ok"
        assert fake.calls[-1]["reasoning_effort"] == "low"

    def test_fix_is_remembered_per_model(self):
        g, fake = gpt(effort_rule("none", ["low", "medium", "high"]),
                      model="gpt-5.4", reasoning_effort="none")
        g.invoke("one")
        g.invoke("two")
        assert len(fake.calls) == 3, "second call repeated the rejected request"

    def test_prefers_minimal_when_offered(self):
        g, fake = gpt(effort_rule("none", ["minimal", "low", "medium", "high"]),
                      model="gpt-5", reasoning_effort="none")
        g.invoke("hi")
        assert fake.calls[-1]["reasoning_effort"] == "minimal"

    def test_xhigh_falls_back_to_high(self):
        g, fake = gpt(effort_rule("xhigh", ["low", "medium", "high"]),
                      model="o3", reasoning_effort="xhigh")
        g.invoke("hi")
        assert fake.calls[-1]["reasoning_effort"] == "high"

    def test_native_tool_calling_path_adapts_too(self):
        g, fake = gpt(effort_rule("none", ["low", "medium", "high"]),
                      model="gpt-5.4", reasoning_effort="none")
        out = g.call_with_tools([{"role": "user", "content": "hi"}],
                                [{"name": "t", "description": "d",
                                  "parameters": {"type": "object", "properties": {}}}])
        assert out["type"] == "text"
        assert fake.calls[-1]["reasoning_effort"] == "low"

    def test_old_model_without_the_parameter(self):
        def rule(kw):
            if sent(kw.get("reasoning_effort")):
                return openai_400("Unrecognized request argument supplied: reasoning_effort",
                                  "unsupported_parameter", None)
        g, fake = gpt(rule, model="gpt-4o", reasoning_effort="low")
        g.invoke("hi")
        assert not sent(fake.calls[-1].get("reasoning_effort"))


class TestMaxTokensAndTemperature:
    @staticmethod
    def _maxtok_rule(kw):
        if sent(kw.get("max_tokens")):
            return openai_400("Unsupported parameter: 'max_tokens' is not supported with this "
                              "model. Use 'max_completion_tokens' instead.",
                              "unsupported_parameter", "max_tokens")

    def test_known_reasoning_model_renamed_up_front(self):
        g, fake = gpt(self._maxtok_rule, model="o4-mini", max_tokens=500)
        g.invoke("hi")
        assert len(fake.calls) == 1, "should not cost a rejected call"
        assert fake.calls[0]["max_completion_tokens"] == 500

    def test_unknown_future_model_learns_the_rename(self):
        g, fake = gpt(self._maxtok_rule, model="gpt-9-omega", max_tokens=500)
        g.invoke("hi")
        assert fake.calls[-1]["max_completion_tokens"] == 500
        assert not sent(fake.calls[-1].get("max_tokens"))

    def test_old_model_keeps_max_tokens(self):
        g, fake = gpt(lambda kw: None, model="gpt-4o", max_tokens=500)
        g.invoke("hi")
        assert fake.calls[0]["max_tokens"] == 500

    def test_temperature_dropped_when_only_default_allowed(self):
        def rule(kw):
            if sent(kw.get("temperature")) and kw["temperature"] != 1:
                return openai_400("Unsupported value: 'temperature' does not support 0.2 with "
                                  "this model. Only the default (1) value is supported.",
                                  "unsupported_value", "temperature")
        g, fake = gpt(rule, model="gpt-5", temperature=0.2)
        g.invoke("hi")
        assert not sent(fake.calls[-1].get("temperature"))


class TestNotOverreaching:
    def test_unrelated_400_still_raises_once(self):
        g, fake = gpt(lambda kw: openai_400("This model's maximum context length is 128000 tokens.",
                                            "context_length_exceeded", "messages"),
                      model="gpt-4o")
        with pytest.raises(openai.BadRequestError):
            g.invoke("hi")
        assert len(fake.calls) == 1

    def test_adapt_params_false_surfaces_raw_error(self):
        g, _ = gpt(effort_rule("none", ["low"]), model="gpt-5.4",
                   reasoning_effort="none", adapt_params=False)
        with pytest.raises(openai.BadRequestError):
            g.invoke("hi")


class _FakeMessages:
    def __init__(self, rule, content=None):
        self.rule, self.calls = rule, []
        self.content = content or [types.SimpleNamespace(type="text", text="ok")]

    def create(self, **kw):
        self.calls.append(kw)
        err = self.rule(kw)
        if err:
            raise err
        return types.SimpleNamespace(content=self.content,
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


def claude(rule, content=None, **kw):
    c = Claude(api_key="x", **kw)
    fake = _FakeMessages(rule, content)
    c.client = types.SimpleNamespace(messages=fake)
    return c, fake


class TestClaude:
    def test_default_sends_no_temperature(self):
        c, fake = claude(lambda kw: None)
        c.invoke("hi")
        assert "temperature" not in fake.calls[0]

    def test_temperature_top_p_conflict_keeps_temperature(self):
        def rule(kw):
            if "temperature" in kw and "top_p" in kw:
                return anthropic_400("`temperature` and `top_p` cannot both be specified for "
                                     "this model. Please use only one.")
        c, fake = claude(rule, temperature=0.3, top_p=0.9)
        c.invoke("hi")
        assert "top_p" not in fake.calls[-1]
        assert fake.calls[-1]["temperature"] == 0.3

    def test_thinking_dropped_on_old_model(self):
        c, fake = claude(lambda kw: anthropic_400("thinking: Extra inputs are not permitted")
                         if "thinking" in kw else None,
                         model="claude-3-haiku-20240307",
                         thinking={"type": "enabled", "budget_tokens": 2000})
        c.invoke("hi")
        assert "thinking" not in fake.calls[-1]

    def test_max_tokens_clamped_to_model_cap(self):
        def rule(kw):
            if kw["max_tokens"] > 4096:
                return anthropic_400("max_tokens: 64000 > 4096, which is the maximum allowed "
                                     "number of output tokens for claude-3-haiku-20240307")
        c, fake = claude(rule, model="claude-3-haiku-20240307", max_tokens=64000)
        c.invoke("hi")
        assert fake.calls[-1]["max_tokens"] == 4096

    def test_thinking_response_returns_the_text_block(self):
        content = [types.SimpleNamespace(type="thinking", thinking="hmm"),
                   types.SimpleNamespace(type="text", text="answer")]
        c, fake = claude(lambda kw: None, content=content,
                         thinking={"type": "enabled", "budget_tokens": 2000}, temperature=0.2)
        assert c.invoke("hi") == "answer"
        assert "temperature" not in fake.calls[0]
