"""Tests for StandardParser and AgentType variants.

Covers the two main entry points:
- ``from_json(text)`` — parse assistant text into a parser instance.
- ``from_function_call(input_dict)`` — parse native FC output.

Plus tolerance for the common LLM mistakes: JSON fences, trailing text,
casing variants of Final_Answer.
"""

import json

import pytest

from agentx_dev import (
    AgentType, AgentRunner, StandardParser,
    React_, ChainOfThought, ZeroShot, FewShot, Instruction_Tuned_,
)
from agentx_dev.Runner.AgentRun import _is_terminal_action


class TestStandardParser:

    def test_from_json_simple(self):
        text = '{"Thought": "t", "action": "calc", "action_input": "1+1"}'
        parsed = StandardParser.from_json(text)
        assert isinstance(parsed, StandardParser)
        assert parsed.action == "calc"

    def test_from_json_with_fences(self):
        text = '```json\n{"Thought": "t", "action": "calc", "action_input": "1+1"}\n```'
        parsed = StandardParser.from_json(text)
        assert isinstance(parsed, StandardParser)
        assert parsed.action == "calc"

    def test_from_json_returns_none_on_plain_text(self):
        parsed = StandardParser.from_json("This is a plain text answer.")
        assert not isinstance(parsed, StandardParser)

    def test_from_function_call(self):
        parsed = StandardParser.from_function_call(
            {"Thought": "t", "action": "calc", "action_input": "1+1"}
        )
        assert parsed.action == "calc"
        assert parsed.action_input == "1+1"


class TestTerminalAction:

    @pytest.mark.parametrize("val", [
        "Final_Answer", "final_answer", "FINAL_ANSWER",
        "Final Answer", "final answer",
        "finalanswer", "FinalAnswer",
        "Final-Answer",
    ])
    def test_recognizes_variants(self, val):
        assert _is_terminal_action(val), f"{val!r} should be terminal"

    @pytest.mark.parametrize("val", [
        "final", "answer", "", None, "calculator",
    ])
    def test_rejects_non_terminals(self, val):
        assert not _is_terminal_action(val), f"{val!r} should NOT be terminal"


class TestAgentTypes:
    """Every AgentType must have a template with the three required placeholders
    and a parser class."""

    @pytest.mark.parametrize("agent_type,parser_cls", [
        (AgentType.ReAct, React_),
        (AgentType.Chain_of_Thought, ChainOfThought),
        (AgentType.Zero_Shot, ZeroShot),
        (AgentType.Few_Shot, FewShot),
        (AgentType.Instruction_Tuned, Instruction_Tuned_),
    ])
    def test_type_has_template_and_parser(self, agent_type, parser_cls):
        # AgentType members are direct AgentFormatter instances (not enums).
        assert hasattr(agent_type, "prompt"), f"{agent_type} missing prompt"
        prompt = agent_type.prompt
        assert "{tools}" in prompt
        assert "{tool_names}" in prompt
        assert "{user_input}" in prompt
        assert agent_type.Agent is parser_cls
