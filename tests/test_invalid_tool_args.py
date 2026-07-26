"""Tests for recovery from malformed-JSON tool arguments.

An agent that emits a Python script (or a Windows path) as a tool
argument produces invalid JSON — `\\d`, `\\U`, `C:\\Users` are all
illegal JSON escapes. The OpenAI adapter used to `json.loads` those
eagerly and `raise`, which unwound the entire agent run and surfaced to
a Supervisor as `ERROR: Invalid \\escape: line 1 column 598`. The agent
never got to use its own retry loop.

Two-part fix, both at the runner layer:
  1. `_parse_tool_arguments` repairs the common case (unescaped
     backslashes) so most malformed args parse with no wasted round-trip.
  2. When repair fails, `call_with_tools` returns a dedicated
     ``invalid_tool_args`` result and the loop feeds the error back as a
     retryable observation instead of crashing.
"""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, AgentType, StandardTool
from agentx_dev.ChatModel import _parse_tool_arguments
from tests.conftest import MockModel

REAL_ANSWER = "Saved 12 links to ./workspace/links.txt."


def build_code_tool():
    return StandardTool(
        func=lambda x: "ran",
        name="run_python",
        description="Execute a Python script.",
    )


class TestParseHelper:

    def test_valid_json_passes_through(self):
        parsed, err = _parse_tool_arguments('{"a": 1, "b": "x"}')
        assert err is None
        assert parsed == {"a": 1, "b": "x"}

    def test_empty_is_empty_dict(self):
        assert _parse_tool_arguments("") == ({}, None)
        assert _parse_tool_arguments(None) == ({}, None)

    def test_unescaped_backslash_in_regex_is_repaired(self):
        # Raw string content: {"code": "re.findall(r'\d+', s)"}
        raw = '{"code": "re.findall(r\'\\d+\', s)"}'
        parsed, err = _parse_tool_arguments(raw)
        assert err is None
        assert parsed["code"] == "re.findall(r'\\d+', s)"

    def test_windows_path_is_repaired(self):
        # Both segments start with letters that are NOT valid JSON escape
        # chars (\P, \d), so the repair recovers the literal path cleanly.
        raw = '{"path": "C:\\Program\\data"}'
        parsed, err = _parse_tool_arguments(raw)
        assert err is None
        assert parsed["path"] == "C:\\Program\\data"

    def test_ambiguous_valid_escape_letter_is_a_known_limit(self):
        # `\b` IS a valid JSON escape (backspace). When a path segment
        # starts with one of b/f/n/r/t/u, the repair cannot tell "literal
        # backslash" from "intended escape" and leaves it as the escape.
        # Documented so the behavior is a decision, not a surprise.
        raw = '{"path": "C:\\bin"}'
        parsed, err = _parse_tool_arguments(raw)
        assert err is None
        assert parsed["path"] == "C:\bin"  # \b decoded to backspace

    def test_valid_escapes_are_preserved(self):
        raw = '{"text": "line1\\nline2\\ttab"}'
        parsed, err = _parse_tool_arguments(raw)
        assert err is None
        assert parsed["text"] == "line1\nline2\ttab"

    def test_unrepairable_json_returns_error(self):
        parsed, err = _parse_tool_arguments('{"code": "def f(:')
        assert parsed is None
        assert err is not None


def _invalid(name):
    return {
        "type": "invalid_tool_args",
        "name": name,
        "id": "bad1",
        "raw": '{"code": "totally broken',
        "error": "Invalid \\escape: line 1 column 598 (char 597)",
    }


class TestFunctionCallingRecovery:

    def test_recovers_after_feedback(self):
        model = MockModel(tool_script=[
            _invalid("ReAct"),
            {"type": "tool_use", "name": "ReAct", "id": "c2", "input": {
                "Thought": "fixed the escaping",
                "action": "Final_Answer",
                "action_input": REAL_ANSWER,
            }},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            use_function_calling=True, max_iterations=5,
        )
        result = runner.invoke("Scrape and save the links.")
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2

    def test_error_is_fed_back_to_the_model(self):
        model = MockModel(tool_script=[
            _invalid("ReAct"),
            {"type": "tool_use", "name": "ReAct", "id": "c2", "input": {
                "Thought": "", "action": "Final_Answer", "action_input": REAL_ANSWER,
            }},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            use_function_calling=True, max_iterations=5,
        )
        runner.invoke("Scrape and save the links.")
        second_call = model.tool_calls_made[1]
        assert any(
            "[framework]" in str(m.get("content", "")) and "JSON" in str(m.get("content", ""))
            for m in second_call
        ), "the malformed-args error should be visible to the model on retry"

    def test_persistent_bad_args_terminate_within_budget(self):
        # Model never fixes its JSON. The loop must not hang — it stops at
        # max_iterations and synthesizes a summary.
        model = MockModel(tool_script=[_invalid("ReAct")] * 10)
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            use_function_calling=True, max_iterations=3,
        )
        result = runner.invoke("Scrape and save the links.")
        assert result is not None
        assert len(model.tool_calls_made) <= 3


class TestNativeBindingRecovery:

    def test_recovers_after_feedback(self):
        model = MockModel(tool_script=[
            _invalid("run_python"),
            {"type": "tool_use", "name": "respond",
             "input": {"answer": REAL_ANSWER}, "id": "c2"},
        ])
        runner = AgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            bind_tools_natively=True, max_iterations=5,
        )
        result = runner.invoke("Scrape and save the links.")
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2


class TestAsyncRecovery:

    def test_function_calling(self):
        model = MockModel(tool_script=[
            _invalid("ReAct"),
            {"type": "tool_use", "name": "ReAct", "id": "c2", "input": {
                "Thought": "", "action": "Final_Answer", "action_input": REAL_ANSWER,
            }},
        ])
        runner = AsyncAgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            use_function_calling=True, max_iterations=5,
        )
        result = asyncio.run(runner.ainvoke("Scrape and save the links."))
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2

    def test_native_binding(self):
        model = MockModel(tool_script=[
            _invalid("run_python"),
            {"type": "tool_use", "name": "respond",
             "input": {"answer": REAL_ANSWER}, "id": "c2"},
        ])
        runner = AsyncAgentRunner(
            model=model, agent=AgentType.ReAct,
            tools=[build_code_tool()], verbose=False,
            bind_tools_natively=True, max_iterations=5,
        )
        result = asyncio.run(runner.ainvoke("Scrape and save the links."))
        assert result.content == REAL_ANSWER
        assert len(model.tool_calls_made) == 2
