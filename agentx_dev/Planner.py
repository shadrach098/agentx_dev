"""
Planner / plan-then-execute pattern for AgentX.

Provides ``PlanningAgent`` and ``AsyncPlanningAgent`` that wrap an underlying
``AgentRunner`` / ``AsyncAgentRunner`` with a two-phase execution model:

Phase 1 (PLAN): A single LLM call produces a structured step-by-step plan
                given the user task and available tools.
Phase 2 (EXECUTE): The underlying runner is invoked with the plan injected
                   into the prompt context, guiding the ReAct loop.

This dramatically improves multi-step task reliability compared to pure ReAct.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from agentx_dev.Agents.Agent import AgentCompletion
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner


# ----------------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------------

class PlannedCompletion(BaseModel):
    """Result produced by a PlanningAgent / AsyncPlanningAgent run."""

    query: str
    plan: List[str] = Field(default_factory=list)
    content: str
    completion: Optional[AgentCompletion] = None


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

PLANNING_PROMPT = """You are a planning agent. Given a user task and a list of available tools, produce a step-by-step plan to solve it.

Available tools:
{tools}

User task: {user_task}

Produce a numbered plan of 1-{max_steps} concrete steps. Each step should be one observable action or decision.

Respond ONLY with valid JSON in this exact format (no code fences, no extra text):

{{
  "plan": [
    "Step 1: <action>",
    "Step 2: <action>",
    ...
  ]
}}
"""

EXECUTION_PREAMBLE = """The user's task has been pre-planned. Follow this plan as a guide while solving it. You may deviate if the plan turns out wrong, but follow it by default.

PLAN:
{plan_text}

USER TASK: {user_task}
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Remove leading ```json / ``` code fences from a model response."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0]
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0]
    return cleaned.strip()


def _parse_plan(response: str, max_steps: int) -> List[str]:
    """Parse the planning LLM response into a list of step strings."""
    cleaned = _strip_code_fences(response)
    try:
        parsed = json.loads(cleaned)
        plan = parsed.get("plan", []) if isinstance(parsed, dict) else []
        return [str(s) for s in plan][:max_steps]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


# ----------------------------------------------------------------------------
# Sync PlanningAgent
# ----------------------------------------------------------------------------

class PlanningAgent:
    """
    A two-phase agent: PLAN then EXECUTE.

    Phase 1: A single LLM call produces a structured plan.
    Phase 2: An underlying ``AgentRunner`` executes the task with the plan
             injected as context.
    """

    def __init__(
        self,
        model: BaseChatModel,
        Agent,
        tools: List,
        max_iterations: int = 8,
        max_plan_steps: int = 8,
        auto_cache: bool = True,
        auto_memory: bool = False,
    ):
        self.model = model
        self.tools = tools
        self.max_plan_steps = max_plan_steps
        self._runner = AgentRunner(
            model=model,
            Agent=Agent,
            tools=tools,
            max_iterations=max_iterations,
            auto_cache=auto_cache,
            auto_memory=auto_memory,
        )

    def _build_tool_block(self) -> str:
        from agentx_dev.Runner.AgentRun import _format_tool_for_prompt
        return "\n".join(
            f"- {t.name}: {_format_tool_for_prompt(t)}" for t in self.tools
        )

    def _make_plan(self, user_task: str) -> List[str]:
        prompt = PLANNING_PROMPT.format(
            tools=self._build_tool_block(),
            user_task=user_task,
            max_steps=self.max_plan_steps,
        )
        response = self.model.Initialize(
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_plan(response, self.max_plan_steps)

    def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict]] = None,
    ) -> PlannedCompletion:
        # Phase 1: Plan
        plan = self._make_plan(user_input)

        # Phase 2: Execute with plan injected
        if plan:
            plan_text = "\n".join(plan)
            augmented_query = EXECUTION_PREAMBLE.format(
                plan_text=plan_text, user_task=user_input
            )
            completion = self._runner.Initialize(
                augmented_query, ChatHistory=ChatHistory
            )
        else:
            # Fallback: no plan, run normally
            completion = self._runner.Initialize(
                user_input, ChatHistory=ChatHistory
            )

        return PlannedCompletion(
            query=user_input,
            plan=plan,
            content=completion.content,
            completion=completion,
        )

    def __repr__(self) -> str:
        return (
            f"<PlanningAgent(tools={[t.name for t in self.tools]}, "
            f"max_plan_steps={self.max_plan_steps}, model={self.model})>"
        )


# ----------------------------------------------------------------------------
# Async PlanningAgent
# ----------------------------------------------------------------------------

class AsyncPlanningAgent:
    """
    Async two-phase agent: PLAN then EXECUTE.

    Same shape as :class:`PlanningAgent`, but uses ``async_initialize`` on the
    model for the plan call and wraps an :class:`AsyncAgentRunner` for the
    execution phase.
    """

    def __init__(
        self,
        model: BaseChatModel,
        Agent,
        tools: List,
        max_iterations: int = 8,
        max_plan_steps: int = 8,
        auto_cache: bool = True,
        auto_memory: bool = False,
    ):
        self.model = model
        self.tools = tools
        self.max_plan_steps = max_plan_steps
        self._runner = AsyncAgentRunner(
            model=model,
            Agent=Agent,
            tools=tools,
            max_iterations=max_iterations,
            auto_cache=auto_cache,
            auto_memory=auto_memory,
        )

    def _build_tool_block(self) -> str:
        from agentx_dev.Runner.AsyncAgentRun import _format_tool_for_prompt
        return "\n".join(
            f"- {t.name}: {_format_tool_for_prompt(t)}" for t in self.tools
        )

    async def _make_plan(self, user_task: str) -> List[str]:
        prompt = PLANNING_PROMPT.format(
            tools=self._build_tool_block(),
            user_task=user_task,
            max_steps=self.max_plan_steps,
        )
        response = await self.model.async_initialize(
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_plan(response, self.max_plan_steps)

    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict]] = None,
    ) -> PlannedCompletion:
        plan = await self._make_plan(user_input)

        if plan:
            plan_text = "\n".join(plan)
            augmented = EXECUTION_PREAMBLE.format(
                plan_text=plan_text, user_task=user_input
            )
            completion = await self._runner.Initialize(
                augmented, ChatHistory=ChatHistory
            )
        else:
            completion = await self._runner.Initialize(
                user_input, ChatHistory=ChatHistory
            )

        return PlannedCompletion(
            query=user_input,
            plan=plan,
            content=completion.content,
            completion=completion,
        )

    def __repr__(self) -> str:
        return (
            f"<AsyncPlanningAgent(tools={[t.name for t in self.tools]}, "
            f"max_plan_steps={self.max_plan_steps}, model={self.model})>"
        )
