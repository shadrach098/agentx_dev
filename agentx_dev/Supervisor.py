"""
Supervisor / multi-agent orchestration layer for AgentX.

Provides ``Supervisor`` and ``AsyncSupervisor`` classes that:
1. Accept a registry of named specialist sub-agents.
2. Use an LLM to decompose a high-level task into sub-tasks and assign each to
   the best specialist.
3. Execute those sub-tasks (sequentially for the sync supervisor, concurrently
   for the async supervisor).
4. Synthesize the sub-agent results into a single final answer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional, Tuple, Union, Any

from pydantic import BaseModel, Field

from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------

class SubtaskResult(BaseModel):
    """Result from a single specialist sub-agent invocation."""

    agent: str
    query: str
    content: str
    error: Optional[str] = None


class SupervisorResult(BaseModel):
    """Aggregate result produced by a Supervisor run."""

    query: str
    content: str  # final synthesized answer
    subtasks: List[SubtaskResult] = Field(default_factory=list)
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

SUPERVISOR_PLAN_PROMPT = """You are a Supervisor coordinating specialist agents to solve a complex task.

Available specialist agents:
{agent_catalog}

User task: {user_task}

Decompose this task into 1-{max_subtasks} sub-tasks. For each sub-task, choose the best specialist agent.
If the task is simple and only needs one agent, return one sub-task.

Respond ONLY with valid JSON in this exact format (no code fences, no extra text):

{{
  "plan": [
    {{"agent": "<agent_name>", "query": "<specific sub-task for this agent>"}},
    ...
  ]
}}
"""

SUPERVISOR_SYNTHESIZE_PROMPT = """You are a Supervisor synthesizing results from specialist agents.

Original user task: {user_task}

Sub-agent results:
{results_block}

Provide a single coherent final answer to the user's original task, drawing on the sub-agent results.
Be concise and direct. Do not mention the sub-agents or the orchestration process.
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Strip ```json / ``` code fences from an LLM response if present."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0]
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0]
    return cleaned.strip()


def _format_results_block(subtask_results: List[SubtaskResult]) -> str:
    """Format the sub-task results for inclusion in the synthesis prompt."""
    return "\n\n".join(
        f"[{r.agent}] Q: {r.query}\nA: {r.content}"
        + (f"\n  ERROR: {r.error}" if r.error else "")
        for r in subtask_results
    )


# ----------------------------------------------------------------------------
# Sync Supervisor
# ----------------------------------------------------------------------------

class Supervisor:
    """
    Synchronous multi-agent supervisor.

    Decomposes a high-level task into sub-tasks, dispatches them to the
    appropriate specialist :class:`AgentRunner`, and synthesizes the
    results into a single final answer.
    """

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, AgentRunner]],
        max_subtasks: int = 5,
    ):
        """
        Args:
            model: LLM used for planning and synthesis.
            agents: Mapping of ``name -> (description, AgentRunner)``.
            max_subtasks: Hard upper bound on the number of planned sub-tasks.
        """
        self.model = model
        self.agents = agents
        self.max_subtasks = max_subtasks

    # -- internal helpers ----------------------------------------------------

    def _build_agent_catalog(self) -> str:
        lines = []
        for name, (desc, _) in self.agents.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def _plan(self, user_task: str) -> List[dict]:
        prompt = SUPERVISOR_PLAN_PROMPT.format(
            agent_catalog=self._build_agent_catalog(),
            user_task=user_task,
            max_subtasks=self.max_subtasks,
        )
        messages = [{"role": "user", "content": prompt}]
        response = self.model.Initialize(messages=messages)

        cleaned = _strip_code_fences(response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        plan = parsed.get("plan", []) or []
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and item.get("agent") in self.agents
            and item.get("query")
        ]
        return filtered[: self.max_subtasks]

    def _synthesize(self, user_task: str, subtask_results: List[SubtaskResult]) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        messages = [{"role": "user", "content": prompt}]
        return self.model.Initialize(messages=messages)

    # -- public API ----------------------------------------------------------

    def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks sequentially, and synthesize."""
        plan = self._plan(user_task)

        if not plan:
            return SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
            )

        subtask_results: List[SubtaskResult] = []
        for item in plan:
            agent_name = item["agent"]
            sub_query = item["query"]
            _, agent_runner = self.agents[agent_name]
            try:
                completion = agent_runner.Initialize(sub_query)
                subtask_results.append(SubtaskResult(
                    agent=agent_name,
                    query=sub_query,
                    content=completion.content,
                ))
            except Exception as e:
                subtask_results.append(SubtaskResult(
                    agent=agent_name,
                    query=sub_query,
                    content="",
                    error=str(e),
                ))

        final = self._synthesize(user_task, subtask_results)

        return SupervisorResult(
            query=user_task,
            content=final,
            subtasks=subtask_results,
            plan=plan,
        )

    def __repr__(self) -> str:
        return (
            f"<Supervisor(agents={list(self.agents.keys())}, "
            f"max_subtasks={self.max_subtasks})>"
        )


# ----------------------------------------------------------------------------
# Async Supervisor
# ----------------------------------------------------------------------------

class AsyncSupervisor:
    """
    Asynchronous multi-agent supervisor.

    Like :class:`Supervisor` but runs sub-tasks concurrently via
    ``asyncio.gather``. Accepts both :class:`AsyncAgentRunner` and
    :class:`AgentRunner` instances (sync runners are wrapped in
    ``asyncio.to_thread``).
    """

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, Union[AgentRunner, AsyncAgentRunner]]],
        max_subtasks: int = 5,
    ):
        self.model = model
        self.agents = agents
        self.max_subtasks = max_subtasks

    # -- internal helpers ----------------------------------------------------

    def _build_agent_catalog(self) -> str:
        lines = []
        for name, (desc, _) in self.agents.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    async def _call_model(self, messages: List[Dict[str, str]]) -> str:
        """Invoke the planning/synthesis model, preferring an async method."""
        if hasattr(self.model, "async_initialize") and asyncio.iscoroutinefunction(
            self.model.async_initialize
        ):
            return await self.model.async_initialize(messages=messages)
        # Fall back to running the sync method in a thread.
        return await asyncio.to_thread(self.model.Initialize, messages)

    async def _plan(self, user_task: str) -> List[dict]:
        prompt = SUPERVISOR_PLAN_PROMPT.format(
            agent_catalog=self._build_agent_catalog(),
            user_task=user_task,
            max_subtasks=self.max_subtasks,
        )
        messages = [{"role": "user", "content": prompt}]
        response = await self._call_model(messages)

        cleaned = _strip_code_fences(response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        plan = parsed.get("plan", []) or []
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and item.get("agent") in self.agents
            and item.get("query")
        ]
        return filtered[: self.max_subtasks]

    async def _synthesize(
        self, user_task: str, subtask_results: List[SubtaskResult]
    ) -> str:
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=user_task,
            results_block=results_block,
        )
        messages = [{"role": "user", "content": prompt}]
        return await self._call_model(messages)

    async def _run_subtask(self, agent_name: str, sub_query: str) -> SubtaskResult:
        _, runner = self.agents[agent_name]
        try:
            initialize = getattr(runner, "Initialize", None)
            if initialize is None:
                raise AttributeError(
                    f"Registered agent '{agent_name}' has no 'Initialize' method."
                )

            if asyncio.iscoroutinefunction(initialize):
                completion = await initialize(sub_query)
            else:
                completion = await asyncio.to_thread(initialize, sub_query)

            return SubtaskResult(
                agent=agent_name,
                query=sub_query,
                content=completion.content,
            )
        except Exception as e:
            return SubtaskResult(
                agent=agent_name,
                query=sub_query,
                content="",
                error=str(e),
            )

    # -- public API ----------------------------------------------------------

    async def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks concurrently, and synthesize."""
        plan = await self._plan(user_task)

        if not plan:
            return SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
            )

        tasks = [
            self._run_subtask(item["agent"], item["query"])
            for item in plan
        ]
        subtask_results = await asyncio.gather(*tasks)

        final = await self._synthesize(user_task, list(subtask_results))

        return SupervisorResult(
            query=user_task,
            content=final,
            subtasks=list(subtask_results),
            plan=plan,
        )

    def __repr__(self) -> str:
        return (
            f"<AsyncSupervisor(agents={list(self.agents.keys())}, "
            f"max_subtasks={self.max_subtasks})>"
        )
