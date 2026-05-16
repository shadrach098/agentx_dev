"""Tests for the PlanningAgent / AsyncPlanningAgent plan-then-execute layer."""

import asyncio
import json

import pytest

from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.Planner import PlanningAgent, AsyncPlanningAgent, PlannedCompletion


class MockModel(BaseChatModel):
    """Mock model that returns scripted responses for each call."""

    def __init__(self, responses):
        self._iter = iter(responses)

    def Initialize(self, messages) -> str:
        return next(self._iter)


def _dummy_tool() -> StandardTool:
    def dummy(x: str) -> str:
        """dummy"""
        return x

    return StandardTool(func=dummy, name="dummy", description="dummy tool.")


def test_planning_agent_produces_plan_and_executes():
    """The agent makes a plan first, then executes it via the underlying runner."""
    plan_resp = json.dumps({"plan": ["Step 1: search", "Step 2: synthesize"]})
    exec_resp = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": "done"}
    )

    model = MockModel([plan_resp, exec_resp])
    tool = _dummy_tool()

    agent = PlanningAgent(model=model, Agent=AgentType.ReAct, tools=[tool])
    result = agent.Initialize("complex task")

    assert isinstance(result, PlannedCompletion)
    assert result.plan == ["Step 1: search", "Step 2: synthesize"]
    assert result.content == "done"


def test_planning_agent_handles_no_plan_gracefully():
    """If planning returns garbage, fall back to direct execution."""
    plan_resp = "not valid json at all"
    exec_resp = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": "fallback"}
    )

    model = MockModel([plan_resp, exec_resp])
    tool = _dummy_tool()

    agent = PlanningAgent(model=model, Agent=AgentType.ReAct, tools=[tool])
    result = agent.Initialize("task")

    assert result.plan == []
    assert result.content == "fallback"


def test_planning_agent_strips_code_fences():
    """Plan JSON wrapped in ```json fences must parse."""
    plan_resp = "```json\n" + json.dumps({"plan": ["A", "B"]}) + "\n```"
    exec_resp = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": "ok"}
    )

    model = MockModel([plan_resp, exec_resp])
    tool = _dummy_tool()

    agent = PlanningAgent(model=model, Agent=AgentType.ReAct, tools=[tool])
    result = agent.Initialize("task")
    assert result.plan == ["A", "B"]


def test_planning_agent_respects_max_plan_steps():
    """If LLM returns more steps than max_plan_steps, truncate."""
    big_plan = json.dumps({"plan": [f"Step {i}" for i in range(20)]})
    exec_resp = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": "ok"}
    )

    model = MockModel([big_plan, exec_resp])
    tool = _dummy_tool()

    agent = PlanningAgent(
        model=model, Agent=AgentType.ReAct, tools=[tool], max_plan_steps=5
    )
    result = agent.Initialize("task")
    assert len(result.plan) == 5


def test_async_planning_agent():
    """AsyncPlanningAgent must work end-to-end."""
    plan_resp = json.dumps({"plan": ["S1", "S2"]})
    exec_resp = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": "async_done"}
    )

    model = MockModel([plan_resp, exec_resp])
    tool = _dummy_tool()

    agent = AsyncPlanningAgent(model=model, Agent=AgentType.ReAct, tools=[tool])
    result = asyncio.run(agent.Initialize("task"))
    assert result.plan == ["S1", "S2"]
    assert result.content == "async_done"
