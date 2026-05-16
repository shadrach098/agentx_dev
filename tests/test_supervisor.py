"""Tests for the Supervisor / AsyncSupervisor multi-agent orchestration layer."""

import asyncio
import json

import pytest

from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.AsyncTools import AsyncStandardTool
from agentx_dev.Supervisor import Supervisor, AsyncSupervisor, SubtaskResult


class MockModel(BaseChatModel):
    """Mock model that returns scripted responses for each call."""

    def __init__(self, responses):
        self._iter = iter(responses)

    def Initialize(self, messages) -> str:
        return next(self._iter)


def _make_simple_agent(final_answer: str) -> AgentRunner:
    """Build a tiny AgentRunner whose first model call returns Final_Answer."""

    def dummy(x: str) -> str:
        """dummy"""
        return x

    tool = StandardTool(func=dummy, name="dummy", description="dummy tool.")
    final = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": final_answer}
    )
    model = MockModel([final])
    return AgentRunner(model=model, Agent=AgentType.ReAct, tools=[tool])


def _make_simple_async_agent(final_answer: str) -> AsyncAgentRunner:
    """Build a tiny AsyncAgentRunner whose first model call returns Final_Answer."""

    async def dummy_async(x: str) -> str:
        """async dummy"""
        return x

    tool = AsyncStandardTool(func=dummy_async, name="d", description="async dummy.")
    final = json.dumps(
        {"Thought": "t", "action": "Final_Answer", "action_input": final_answer}
    )
    model = MockModel([final])
    return AsyncAgentRunner(model=model, Agent=AgentType.ReAct, tools=[tool])


def test_supervisor_plans_and_synthesizes():
    """Plan into 2 sub-tasks, both run, then synthesize."""
    plan_response = json.dumps({
        "plan": [
            {"agent": "search", "query": "find X"},
            {"agent": "math", "query": "compute Y"},
        ]
    })
    synth_response = "Final synthesized answer."
    supervisor_model = MockModel([plan_response, synth_response])

    search_agent = _make_simple_agent("X is found")
    math_agent = _make_simple_agent("Y equals 42")

    sup = Supervisor(
        model=supervisor_model,
        agents={
            "search": ("Web search.", search_agent),
            "math": ("Calculations.", math_agent),
        },
    )

    result = sup.run("Do something complex")
    assert result.content == "Final synthesized answer."
    assert len(result.subtasks) == 2
    assert result.subtasks[0].agent == "search"
    assert result.subtasks[0].content == "X is found"
    assert result.subtasks[1].agent == "math"
    assert result.subtasks[1].content == "Y equals 42"
    assert len(result.plan) == 2


def test_supervisor_filters_unknown_agents():
    """Plan referencing an unknown agent should drop that entry."""
    plan_response = json.dumps({
        "plan": [
            {"agent": "search", "query": "find X"},
            {"agent": "unknown", "query": "nope"},
        ]
    })
    synth_response = "synth"
    supervisor_model = MockModel([plan_response, synth_response])

    search_agent = _make_simple_agent("X is found")

    sup = Supervisor(
        model=supervisor_model,
        agents={"search": ("Web search.", search_agent)},
    )

    result = sup.run("task")
    assert len(result.plan) == 1
    assert result.plan[0]["agent"] == "search"
    assert len(result.subtasks) == 1
    assert result.subtasks[0].content == "X is found"


def test_supervisor_strips_code_fences_from_plan():
    """Plan wrapped in ```json fences must still parse."""
    plan_payload = json.dumps({"plan": [{"agent": "search", "query": "q"}]})
    plan_response = "```json\n" + plan_payload + "\n```"
    synth_response = "synth"
    supervisor_model = MockModel([plan_response, synth_response])

    search_agent = _make_simple_agent("found")

    sup = Supervisor(
        model=supervisor_model,
        agents={"search": ("Web search.", search_agent)},
    )

    result = sup.run("task")
    assert len(result.subtasks) == 1
    assert result.subtasks[0].content == "found"


def test_async_supervisor_runs_subtasks_concurrently():
    """AsyncSupervisor.run() must work and accept async agents."""
    plan_response = json.dumps({
        "plan": [
            {"agent": "a", "query": "q1"},
            {"agent": "b", "query": "q2"},
        ]
    })
    synth_response = "Final."
    supervisor_model = MockModel([plan_response, synth_response])

    agent_a = _make_simple_async_agent("answer_a")
    agent_b = _make_simple_async_agent("answer_b")

    sup = AsyncSupervisor(
        model=supervisor_model,
        agents={"a": ("agent a", agent_a), "b": ("agent b", agent_b)},
    )

    result = asyncio.run(sup.run("task"))
    assert result.content == "Final."
    assert len(result.subtasks) == 2
    contents = {sr.content for sr in result.subtasks}
    assert "answer_a" in contents
    assert "answer_b" in contents
