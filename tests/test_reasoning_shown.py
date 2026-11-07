"""The model's readable reasoning: asked for (Claude: display "summarized" on models that think anyway; GPT: a
reasoning summary on the Responses API), returned by call_with_tools, yielded by the runner, shown by the screens."""

import io
from types import SimpleNamespace

import pytest

from agentx_dev import ChatModel
from agentx_dev.ChatModel import _anthropic_reasoning, _parse_responses_output, reasoning_display_config


def claude(model, **kw):
    pytest.importorskip("anthropic")
    return ChatModel.Claude(model=model, api_key="test-key", **kw)


class TestClaude:
    def test_only_models_that_think_anyway_get_readable_reasoning(self):
        assert reasoning_display_config("claude-opus-5-5") == {"type": "adaptive", "display": "summarized"}
        assert reasoning_display_config("claude-sonnet-5-5") == {"type": "adaptive", "display": "summarized"}
        assert reasoning_display_config("claude-opus-4-8") is None       # would change the request there
        assert reasoning_display_config("claude-haiku-4-5") is None

    def test_the_request_asks_for_it_only_when_turned_on(self):
        assert claude("claude-opus-5-5", show_reasoning=True)._request_kwargs()["thinking"] == \
            {"type": "adaptive", "display": "summarized"}
        assert "thinking" not in claude("claude-opus-5-5")._request_kwargs()
        assert "thinking" not in claude("claude-opus-4-8", show_reasoning=True)._request_kwargs()
        given = {"type": "adaptive"}
        assert claude("claude-opus-5-5", show_reasoning=True, thinking=given)._request_kwargs()["thinking"] == given

    def test_thinking_blocks_become_the_reasoning(self):
        response = SimpleNamespace(content=[
            SimpleNamespace(type="thinking", thinking="The link carries the session in its hash.", signature="s"),
            SimpleNamespace(type="thinking", thinking="", signature="s"),        # "omitted": nothing to show
            SimpleNamespace(type="tool_use", name="step", input={}, id="t1"),
        ])
        assert _anthropic_reasoning(response) == "The link carries the session in its hash."

    def test_call_with_tools_returns_it(self, monkeypatch):
        model = claude("claude-opus-5-5", show_reasoning=True)
        sent = {}
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="Read the client first.", signature="s"),
                     SimpleNamespace(type="tool_use", name="step", input={"action": "read_path"}, id="t1")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
            stop_reason="tool_use")
        monkeypatch.setattr(model, "_create", lambda client, **kw: sent.update(kw) or response)
        out = model.call_with_tools(messages=[{"role": "user", "content": "hi"}],
                                    tools=[{"name": "step", "description": "d",
                                            "parameters": {"type": "object", "properties": {}}}],
                                    force_tool="step")
        assert out["type"] == "tool_use" and out["reasoning"] == "Read the client first."
        assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}


class TestGPT:
    def test_a_reasoning_summary_becomes_the_reasoning(self):
        response = SimpleNamespace(output=[
            SimpleNamespace(type="reasoning", summary=[SimpleNamespace(type="summary_text", text="Check the hash.")]),
            SimpleNamespace(type="function_call", name="step", arguments="{}", call_id="c1"),
        ])
        out = _parse_responses_output(response)
        assert out["type"] == "tool_use" and out["reasoning"] == "Check the hash."
        assert "reasoning" not in _parse_responses_output(SimpleNamespace(output=[]))

    def _gpt(self, monkeypatch, fail_first=False):
        pytest.importorskip("openai")
        model = ChatModel.GPT(model="gpt-6.1-sol", api_key="test-key", show_reasoning=True)
        calls = []

        class Rejected(Exception):
            status_code = 400

        def create(**kw):
            calls.append(kw)
            if fail_first and len(calls) == 1:
                raise Rejected("Your organization must be verified to generate reasoning summaries.")
            return SimpleNamespace(output=[])
        monkeypatch.setattr(model, "client", SimpleNamespace(responses=SimpleNamespace(create=create)))
        monkeypatch.setattr(ChatModel, "_is_openai_reasoning_model", lambda name: True)
        return model, calls

    def test_the_responses_request_asks_for_a_summary(self, monkeypatch):
        ChatModel._NO_REASONING_SUMMARY.discard("gpt-6.1-sol")
        model, calls = self._gpt(monkeypatch)
        model._responses_create_flat(model="gpt-6.1-sol", input=[], reasoning_effort="high")
        assert calls[0]["reasoning"] == {"effort": "high", "summary": "auto"}

    def test_an_account_without_summaries_is_asked_once(self, monkeypatch):
        ChatModel._NO_REASONING_SUMMARY.discard("gpt-6.1-sol")
        model, calls = self._gpt(monkeypatch, fail_first=True)
        model._responses_create_flat(model="gpt-6.1-sol", input=[], reasoning_effort="high")
        assert calls[0]["reasoning"]["summary"] == "auto" and calls[1]["reasoning"] == {"effort": "high"}
        model._responses_create_flat(model="gpt-6.1-sol", input=[])
        assert "reasoning" not in calls[2]                                  # not asked again, effort untouched
        ChatModel._NO_REASONING_SUMMARY.discard("gpt-6.1-sol")


def test_the_cli_builds_its_models_with_reasoning_shown(monkeypatch):
    pytest.importorskip("anthropic")
    from agentx_dev.cli.models import choose_model
    model = choose_model("claude-opus-5-5", {"ANTHROPIC_API_KEY": "k"})
    assert model.show_reasoning is True


def test_the_runner_yields_the_reasoning_before_the_step():
    from agentx_dev import AgentRunner, AgentType
    from tests.conftest import MockModel

    class Thinks(MockModel):
        def __init__(self):
            super().__init__(script=[])
            self.turn = 0

        def call_with_tools(self, messages, tools, *, force_tool=None):
            self.turn += 1
            if self.turn == 1:
                return {"type": "tool_use", "name": force_tool or "step", "id": "t1", "reasoning": "First, answer.",
                        "input": {"Thought": "", "action": "Final_Answer", "action_input": "done"},
                        "tool_calls": []}
            return {"type": "text", "text": "done"}

    runner = AgentRunner(Thinks(), agent=AgentType.ReAct, tools=[], verbose=False)
    events = list(runner.stream("hi"))
    kinds = [e.get("type") for e in events if isinstance(e, dict)]
    assert "reasoning" in kinds
    assert next(e for e in events if isinstance(e, dict) and e.get("type") == "reasoning")["content"] == "First, answer."


def test_the_window_and_the_terminal_show_it():
    from rich.console import Console
    from agentx_dev.cli.ui import Screen
    from agentx_dev.cli.webui import Bus, WebScreen
    bus = Bus()
    WebScreen(Console(file=io.StringIO()), bus).show_event({"type": "reasoning", "content": "Check the hash."})
    event = bus.after(0)[-1]
    assert event["type"] == "thought" and event["text"] == "Check the hash." and event["reasoning"] is True
    out = io.StringIO()
    screen = Screen(Console(file=out, width=100))
    screen.show_event({"type": "reasoning", "content": "Turn on detectSessionInUrl."})
    screen.show_event({"type": "tool_call", "name": "edit_file", "args": {"path": "a.ts"}})
    assert "Turn on detectSessionInUrl." in out.getvalue()                # before an edit: shown, like a note
