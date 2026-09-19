"""media= on chat models and structured output; structured output on a
model that needs the Responses API; quieter switch logging."""

import asyncio
import json
import logging
import types

import pytest
from pydantic import BaseModel, Field

from agentx_dev import Claude, GPT, Media
from tests.test_responses_fallback import FakeOpenAI, MODEL, _400


class DocumentVerificationResult(BaseModel):
    is_valid: bool = Field(description="True when the document passes every check")
    issues: list[str] = Field(default_factory=list, description="Problems found")


VERDICT = {"is_valid": True, "issues": []}


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "id_card.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return str(path)


# --------------------------------------------------------------- fakes
def fake_gpt(model="gpt-4o", **kw):
    g = GPT(api_key="x", model=model, **kw)
    calls = []

    def create(**k):
        calls.append(k)
        if k.get("tools"):
            tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(
                name=k["tools"][0]["function"]["name"], arguments=json.dumps(VERDICT)))
            msg = types.SimpleNamespace(content=None, tool_calls=[tc])
        else:
            msg = types.SimpleNamespace(content="a photo of an ID card", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    g.client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))
    return g, calls


def fake_claude():
    c = Claude(api_key="x")
    calls = []

    def respond(k):
        calls.append(k)
        if k.get("tools"):
            block = types.SimpleNamespace(type="tool_use", name=k["tools"][0]["name"],
                                          input=VERDICT, id="tu_1")
        else:
            block = types.SimpleNamespace(type="text", text="a photo of an ID card")
        return types.SimpleNamespace(content=[block],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    async def acreate(**k):
        return respond(k)

    c.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **k: respond(k)))
    real = c._anthropic
    c._anthropic = types.SimpleNamespace(
        NOT_GIVEN=real.NOT_GIVEN,
        AsyncAnthropic=lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(create=acreate)))
    return c, calls


def last_user(messages):
    return [m for m in messages if m.get("role") == "user"][-1]["content"]


# --------------------------------------------------------- invoke(media=)
class TestChatModelMedia:
    def test_gpt_invoke_media(self, png):
        g, calls = fake_gpt()
        assert g.invoke("What is this?", media=[png]) == "a photo of an ID card"
        parts = last_user(calls[-1]["messages"])
        assert parts[0] == {"type": "text", "text": "What is this?"}
        assert parts[1]["type"] == "image_url"

    def test_claude_invoke_media(self, png):
        c, calls = fake_claude()
        c.invoke("What is this?", media=[Media.image(png)])
        assert [b["type"] for b in calls[-1]["messages"][-1]["content"]] == ["text", "image"]

    def test_async_invoke_media_both_providers(self, png):
        g, gcalls = fake_gpt()
        c, ccalls = fake_claude()
        asyncio.run(g.ainvoke("What is this?", media=[png]))
        asyncio.run(c.ainvoke("What is this?", media=[png]))
        assert last_user(gcalls[-1]["messages"])[1]["type"] == "image_url"
        assert ccalls[-1]["messages"][-1]["content"][1]["type"] == "image"

    def test_media_appends_to_an_existing_parts_list(self, png):
        g, calls = fake_gpt()
        g.invoke([{"role": "user", "content": [{"type": "text", "text": "Compare"},
                                               Media.image(png)]}], media=[png])
        assert [p["type"] for p in last_user(calls[-1]["messages"])] == ["text", "image_url", "image_url"]

    def test_callers_messages_not_mutated(self, png):
        g, _ = fake_gpt()
        msgs = [{"role": "user", "content": "What is this?"}]
        g.invoke(msgs, media=[png])
        assert msgs == [{"role": "user", "content": "What is this?"}]


# ---------------------------------------------- with_structured_output
class TestStructuredOutputMedia:
    def test_gpt_structured_with_media(self, png):
        g, calls = fake_gpt()
        out = g.with_structured_output(DocumentVerificationResult).invoke(
            "Verify this ID card.", media=[png])
        assert out == DocumentVerificationResult(**VERDICT)
        assert last_user(calls[-1]["messages"])[1]["type"] == "image_url"

    def test_claude_structured_with_media(self, png):
        c, calls = fake_claude()
        out = c.with_structured_output(DocumentVerificationResult).invoke(
            "Verify this ID card.", media=[png])
        assert out.is_valid is True
        assert calls[-1]["messages"][-1]["content"][1]["type"] == "image"

    def test_async_structured_with_media(self, png):
        c, _ = fake_claude()
        out = asyncio.run(c.with_structured_output(DocumentVerificationResult)
                          .ainvoke("Verify this ID card.", media=[png]))
        assert out.is_valid is True


def astra_verdict():
    return [types.SimpleNamespace(type="function_call", name="DocumentVerificationResult",
                                  call_id="call_v", id="fc_v", arguments=json.dumps(VERDICT))]


class TestStructuredOutputOnResponsesModel:
    """The reported case: with_structured_output on gpt-6-astra."""

    def _astra(self, **kw):
        g = GPT(api_key="x", model=MODEL, reasoning_effort="none", **kw)
        fake = FakeOpenAI(responses_output=astra_verdict())
        g.client = fake
        return g, fake

    def test_structured_output_switches_and_parses(self, png):
        g, fake = self._astra()
        out = g.with_structured_output(DocumentVerificationResult).invoke(
            "Verify this ID card.", media=[png])
        assert out == DocumentVerificationResult(**VERDICT)
        sent = fake.responses_calls[-1]
        assert sent["tool_choice"] == {"type": "function", "name": "DocumentVerificationResult"}
        assert [p["type"] for p in sent["input"][-1]["content"]] == ["input_text", "input_image"]

    def test_rejected_forced_tool_choice_is_relaxed_and_remembered(self):
        g, fake = self._astra()
        original = fake._responses

        def picky(**kw):
            if isinstance(kw.get("tool_choice"), dict):
                fake.responses_calls.append(kw)
                raise _400("tool_choice forcing a specific function is not supported with "
                           "reasoning for this model.", "unsupported_value", "tool_choice",
                           path="responses")
            return original(**kw)

        fake.responses.create = picky
        runnable = g.with_structured_output(DocumentVerificationResult)
        assert runnable.invoke("Verify.") == DocumentVerificationResult(**VERDICT)
        assert fake.responses_calls[-1]["tool_choice"] == "required"
        before = len(fake.responses_calls)
        runnable.invoke("Verify again.")
        assert len(fake.responses_calls) == before + 1, "relaxed choice should be remembered"
        assert fake.responses_calls[-1]["tool_choice"] == "required"

    def test_json_text_answer_accepted_when_tool_choice_is_auto(self):
        g, fake = self._astra()
        fake.responses_output = [types.SimpleNamespace(type="message", content=[
            types.SimpleNamespace(type="output_text", text=json.dumps(VERDICT))])]
        out = g.with_structured_output(DocumentVerificationResult).invoke("Verify.")
        assert out.is_valid is True

    def test_successful_switch_does_not_log_the_old_error_as_a_failure(self, caplog):
        g, _ = self._astra()
        with caplog.at_level(logging.WARNING, logger="agentx_dev.ChatModel"):
            g.with_structured_output(DocumentVerificationResult).invoke("Verify.")
        messages = [r.getMessage() for r in caplog.records]
        assert not any("non-retryable" in m for m in messages), messages
        assert not any(r.levelno >= logging.ERROR for r in caplog.records), messages
        assert any("switching its tool calls to the Responses API" in m for m in messages)
