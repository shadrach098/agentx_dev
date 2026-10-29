"""Claude declines a request whose step tool asks it to write out its reasoning (stop_reason "refusal", category
"reasoning_extraction"; seen on claude-opus-5 and claude-opus-5-5 for every agentx request). The step tool goes
without its Thought field, and a decline, or max_tokens used up with no answer, is an error that says so instead of
an empty answer."""

import asyncio
import types

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, Claude
from agentx_dev.ChatModel import ModelDeclined

DETAILS = types.SimpleNamespace(type="refusal", category="reasoning_extraction",
                                explanation="This request was blocked as it seems to violate the terms.")


def response(content, stop="end_turn", details=None, out=10):
    return types.SimpleNamespace(content=content, stop_reason=stop, stop_details=details,
                                 usage=types.SimpleNamespace(input_tokens=100, output_tokens=out))


def tool_use(name, data):
    return types.SimpleNamespace(type="tool_use", name=name, input=data, id="t1")


class LikeTheApi:
    """Declines when any tool still has a Thought field, as claude-opus-5-5 did; otherwise answers 4."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if any("Thought" in (t.get("input_schema") or {}).get("properties", {}) for t in kw.get("tools") or []):
            return response([], stop="refusal", details=DETAILS, out=0)
        step = kw["tools"][0]["name"]
        return response([tool_use(step, {"action": "Final_Answer", "action_input": "2 + 2 = 4"})], stop="tool_use")


def claude(api, model="claude-opus-5-5"):
    c = Claude(api_key="x", model=model)
    c.client = types.SimpleNamespace(messages=api)
    return c


class TestTheStepTool:
    @pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-5", "claude-sonnet-4-6"])
    def test_an_agent_run_works_because_the_step_tool_has_no_thought_field(self, model):
        api = LikeTheApi()
        runner = AgentRunner(model=claude(api, model), agent=AgentType.ReAct, tools=[], verbose=False)
        assert runner.invoke("What is 2+2?").content == "2 + 2 = 4"
        sent = api.calls[0]["tools"][0]
        assert "Thought" not in sent["input_schema"]["properties"] and "thought" not in sent["description"].lower()
        assert set(sent["input_schema"]["properties"]) == {"action", "action_input"}

    def test_the_async_runner_too(self):
        api = LikeTheApi()

        class AsyncApi:
            async def create(self, **kw):
                return api.create(**kw)
        model = claude(api)
        model._anthropic = types.SimpleNamespace(AsyncAnthropic=lambda **kw: types.SimpleNamespace(messages=AsyncApi()))
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        assert asyncio.run(runner.ainvoke("What is 2+2?")).content == "2 + 2 = 4"

    def test_other_tools_keep_their_fields(self):
        from agentx_dev.ChatModel import _without_thought
        tools = [{"name": "React_", "description": "d", "parameters": {"properties": {"Thought": {}, "action": {}},
                                                                       "required": ["Thought", "action"]}},
                 {"name": "notes", "description": "d", "parameters": {"properties": {"Thought": {}}}}]
        out = _without_thought(tools, "React_")
        assert out[0]["parameters"]["properties"] == {"action": {}} and out[0]["parameters"]["required"] == ["action"]
        assert out[1] is tools[1] and "Thought" in tools[0]["parameters"]["properties"]      # the caller's list is kept
        assert _without_thought(tools, None) is tools


class TestADeclineIsAnError:
    def test_a_refusal_says_why(self):
        api = types.SimpleNamespace(create=lambda **kw: response([], stop="refusal", details=DETAILS, out=0))
        with pytest.raises(ModelDeclined) as info:
            claude(api).Initialize([{"role": "user", "content": "hi"}])
        assert info.value.reason == "refusal" and info.value.category == "reasoning_extraction"
        assert "claude-opus-5-5 declined this request (reasoning_extraction)" in str(info.value)

    def test_max_tokens_with_nothing_written_says_so(self):
        api = types.SimpleNamespace(create=lambda **kw: response([], stop="max_tokens", out=16000))
        with pytest.raises(ModelDeclined, match="max_tokens"):
            claude(api).Initialize([{"role": "user", "content": "hi"}])

    def test_max_tokens_with_an_answer_is_still_an_answer(self):
        text = types.SimpleNamespace(type="text", text="a long answer, cut")
        api = types.SimpleNamespace(create=lambda **kw: response([text], stop="max_tokens"))
        assert claude(api).Initialize([{"role": "user", "content": "hi"}]) == "a long answer, cut"

    def test_an_agent_run_reports_the_decline_instead_of_an_empty_answer(self):
        api = types.SimpleNamespace(create=lambda **kw: response([], stop="refusal", details=DETAILS, out=0))
        runner = AgentRunner(model=claude(api), agent=AgentType.ReAct, tools=[], verbose=False)
        with pytest.raises(ModelDeclined):
            runner.invoke("hi")
