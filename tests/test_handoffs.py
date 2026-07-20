"""Tests for HandoffRequest + handoff_tool + HandoffCoordinator."""

import pytest

from agentx_dev import (
    HandoffRequest, HandoffCoordinator, HandoffResult, handoff_tool,
)
from agentx_dev.Agents.Agent import AgentCompletion, ToolCall


class MockRunner:
    """Runner-shaped object for coordinator tests."""

    def __init__(self, name, next_step=None, final="done"):
        self.name = name
        self.next_step = next_step
        self.final = final
        self.calls = []

    def invoke(self, query, chat_history=None):
        self.calls.append({"query": query, "history_len": len(chat_history or [])})
        tcs = []
        if self.next_step:
            tcs = [ToolCall(
                name=f"handoff_to_{self.next_step}",
                args={},
                result=f"HANDOFF -> {self.next_step}: {query}",
            )]
        content = self.final if not self.next_step else f"[{self.name}] handing off"
        return AgentCompletion.from_agent(
            model_name="mock", query=query, content=content,
            tool_calls=tcs, steps=[], history=[
                {"role": "user", "content": query},
                {"role": "assistant", "content": content},
            ],
        )


class TestHandoffTool:

    def test_returns_handoff_request(self):
        t = handoff_tool("researcher")
        r = t.func(task="find MVCC info")
        assert isinstance(r, HandoffRequest)
        assert r.target == "researcher"
        assert r.task == "find MVCC info"

    def test_string_repr_has_prefix(self):
        r = HandoffRequest(target="X", task="Y")
        assert str(r).startswith("HANDOFF -> X")

    def test_custom_tool_name(self):
        t = handoff_tool("writer", tool_name="delegate_writing")
        assert t.name == "delegate_writing"


class TestCoordinator:

    def test_single_hop(self):
        coord = HandoffCoordinator(
            {
                "triage": MockRunner("triage", next_step="writer"),
                "writer": MockRunner("writer", final="Final draft."),
            },
            entry="triage",
        )
        result = coord.run("Draft it.")
        assert isinstance(result, HandoffResult)
        assert result.content == "Final draft."
        assert len(result.hops) == 1
        assert result.hops[0]["from"] == "triage"
        assert result.hops[0]["to"] == "writer"

    def test_no_handoff_returns_directly(self):
        coord = HandoffCoordinator(
            {"solo": MockRunner("solo", final="done")}, entry="solo",
        )
        result = coord.run("anything")
        assert result.hops == []
        assert result.content == "done"

    def test_max_hops_bounds_loop(self):
        # A loops to B, B loops to A -> infinite ping-pong without bounds.
        coord = HandoffCoordinator(
            {
                "A": MockRunner("A", next_step="B"),
                "B": MockRunner("B", next_step="A"),
            },
            entry="A",
            max_hops=3,
        )
        result = coord.run("start")
        # Should terminate cleanly with the loop-exceeded message.
        assert "max_hops" in result.content.lower() or "exceeded" in result.content.lower()

    def test_unknown_target_returns_gracefully(self):
        coord = HandoffCoordinator(
            {"A": MockRunner("A", next_step="ghost")},
            entry="A",
        )
        result = coord.run("start")
        assert "unknown" in result.content.lower()

    def test_history_sanitized_between_hops(self):
        writer = MockRunner("writer", final="fin")
        coord = HandoffCoordinator(
            {
                "triage": MockRunner("triage", next_step="writer"),
                "writer": writer,
            },
            entry="triage",
        )
        coord.run("query")
        # Writer received a history but only clean user/assistant text.
        assert len(writer.calls) == 1

    def test_entry_must_exist(self):
        with pytest.raises(ValueError):
            HandoffCoordinator({"a": MockRunner("a")}, entry="nope")


class TestSanitizer:

    def test_drops_tool_and_function_roles(self):
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "name": "t", "content": "res", "tool_call_id": "x"},
            {"role": "function", "name": "t", "content": "res"},
            {"role": "user", "content": "u2"},
        ]
        clean = HandoffCoordinator._sanitize_history_for_next_agent(history)
        roles = [m["role"] for m in clean]
        assert roles == ["user", "assistant", "user"]
        # Check no tool_call_ids leak
        assert all("tool_call_id" not in m for m in clean)

    def test_empty_history_returns_none(self):
        assert HandoffCoordinator._sanitize_history_for_next_agent(None) is None
        assert HandoffCoordinator._sanitize_history_for_next_agent([]) is None
