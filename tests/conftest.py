"""Shared pytest fixtures.

MockModel implements just enough of BaseChatModel for the runner + parser
tests to exercise every code path without an actual LLM key. Behavior is
scripted per-test.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, List, Optional

import pytest

from agentx_dev import BaseChatModel


class MockModel(BaseChatModel):
    """Scripted chat model.

    ``script`` is either:
      - a list of strings — each `Initialize` call pops the next string.
      - a callable ``(messages) -> str`` — full control per call.

    ``tool_script`` is the analogous shape for ``call_with_tools``: each
    call returns the next dict.
    """

    def __init__(
        self,
        script: Any = None,
        tool_script: Optional[List[dict]] = None,
    ):
        self._script = script or []
        self._script_fn = script if callable(script) else None
        self._tool_script = list(tool_script or [])
        self.calls: List[List[dict]] = []
        self.tool_calls_made: List[List[dict]] = []

    def Initialize(self, messages) -> str:
        self.calls.append(list(messages))
        self._record_usage_counts(input_tokens=10, output_tokens=5)
        if self._script_fn is not None:
            return self._script_fn(messages)
        if isinstance(self._script, list) and self._script:
            return self._script.pop(0)
        return ""

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tool_calls_made.append(list(messages))
        self._record_usage_counts(input_tokens=10, output_tokens=5)
        if not self._tool_script:
            return {"type": "text", "text": ""}
        return self._tool_script.pop(0)

    async def async_initialize(self, messages) -> str:
        return self.Initialize(messages)

    async def async_call_with_tools(self, messages, tools, *, force_tool=None):
        return self.call_with_tools(messages, tools, force_tool=force_tool)


@pytest.fixture
def mock_model():
    return MockModel()


@pytest.fixture
def tmp_workspace(tmp_path):
    """A fresh workspace directory scoped to the test."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def make_react_response(action: str, action_input: Any, thought: str = "") -> str:
    """Build a JSON blob shaped like a ReAct parser response."""
    return json.dumps({
        "Thought": thought or "Thinking...",
        "action": action,
        "action_input": action_input,
    })


def make_final(text: str) -> str:
    return make_react_response("Final_Answer", text)


@pytest.fixture
def make_react():
    return make_react_response


@pytest.fixture
def make_final_response():
    return make_final
