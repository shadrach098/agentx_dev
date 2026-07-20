"""
Agent-to-agent handoffs for AgentX.

The ``Supervisor`` orchestration pattern is top-down: one planner decomposes
a task into sub-tasks and dispatches each to a named specialist. That works
when the shape of the plan is knowable upfront.

Handoffs are the complementary pattern for when it isn't. A running agent
decides mid-task that a peer specialist should take over -- the current
agent calls a ``handoff_to_<name>`` tool, its loop terminates cleanly
with a ``HandoffRequest``, and the ``HandoffCoordinator`` re-invokes the
target with the same conversation. Modeled after OpenAI Swarm's transfer
functions and Anthropic's agent-SDK routing.

Usage:

    from agentx_dev import AgentRunner, AgentType, Claude, Permissions
    from agentx_dev.Handoffs import HandoffCoordinator, handoff_tool

    triage = AgentRunner(
        model=Claude(), agent=AgentType.ReAct,
        tools=[handoff_tool("researcher", "Delegate deep research questions."),
               handoff_tool("writer", "Delegate final drafting.")],
    )
    researcher = AgentRunner(...)  # can also handoff back to writer, etc.
    writer = AgentRunner(...)

    coord = HandoffCoordinator({
        "triage": triage, "researcher": researcher, "writer": writer,
    }, entry="triage")

    result = coord.run("Write a 200-word summary of MVCC in Postgres.")
    print(result.content)

Design points:

- The handoff tool is intentionally dumb: it just packages the args into
  a ``HandoffRequest`` and returns it. The runner detects the sentinel
  return and exits the loop; it does NOT invoke the target. The
  coordinator does -- this keeps runners independent (an AgentRunner
  never needs to know about its siblings).

- ``HandoffCoordinator`` runs a bounded loop between agents. ``max_hops``
  caps the total number of handoffs so two agents can't infinitely
  ping-pong. A ``handoff_history`` is kept on the final completion so
  callers can audit the routing.

- Chat history propagates: when A hands off to B, B's ``invoke`` gets
  A's working history as ``chat_history=``. B can see what A already
  knew. This is the whole point of handoffs vs. Supervisor
  decomposition -- no re-explaining the task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agentx_dev.Agents.Agent import AgentCompletion


__all__ = [
    "HandoffRequest",
    "HandoffCoordinator",
    "HandoffResult",
    "handoff_tool",
    "MAX_HANDOFF_HOPS",
]


MAX_HANDOFF_HOPS = 8


@dataclass
class HandoffRequest:
    """Sentinel value a handoff tool returns when the LLM invokes it.

    The runner's dispatch loop recognizes this type and terminates its
    own run early -- no further tool calls, the ``AgentCompletion.content``
    is set to a status message noting the target, and the coordinator
    catches the completion + request pair and re-invokes the target.
    """
    target: str
    task: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"HANDOFF -> {self.target}: {self.task or '(no task text)'}"


@dataclass
class HandoffResult:
    """What ``HandoffCoordinator.run`` returns.

    Wraps the FINAL agent's completion (the one that answered without
    handing off further) plus an ordered ``hops`` list documenting every
    handoff that occurred. ``hops`` is empty when the entry agent
    answered directly. Callers who want the same shape as
    ``runner.invoke`` can just read ``.completion``.
    """
    completion: AgentCompletion
    hops: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def content(self) -> str:
        return self.completion.content

    @property
    def tool_calls(self):
        return self.completion.tool_calls


def handoff_tool(
    target: str,
    description: Optional[str] = None,
    *,
    tool_name: Optional[str] = None,
):
    """Build a StructuredTool that emits a HandoffRequest.

    Args:
        target: The name of the specialist the coordinator will route to.
            Must match a key in ``HandoffCoordinator.agents``.
        description: What the LLM sees explaining when to use this tool.
            Defaults to a generic ``Hand off to <target>`` -- override with
            a specific hint like "for deep research questions".
        tool_name: The name the LLM calls (defaults to ``handoff_to_<target>``).
    """
    from pydantic import BaseModel, Field
    from agentx_dev.Tools import StructuredTool

    name = tool_name or f"handoff_to_{target}"

    class HandoffArgs(BaseModel):
        task: str = Field(
            ...,
            description=(
                "The task or question the target agent should handle. "
                "Include enough context that the target can act "
                "immediately."
            ),
        )
        rationale: str = Field(
            "",
            description=(
                "Optional short explanation of why this handoff is the "
                "right call. Not shown to the user; useful for tracing."
            ),
        )

    def _do_handoff(task: str, rationale: str = "") -> HandoffRequest:
        return HandoffRequest(
            target=target,
            task=task,
            context={"rationale": rationale} if rationale else {},
        )

    return StructuredTool(
        func=_do_handoff,
        args_schema=HandoffArgs,
        name=name,
        description=description or (
            f"Hand off the conversation to the '{target}' specialist. "
            f"Use when the current work is outside your scope and the "
            f"'{target}' specialist is better suited."
        ),
    )


class HandoffCoordinator:
    """Route between AgentRunners via HandoffRequest sentinels.

    Given a dict of ``{name: runner}`` and an ``entry`` agent name,
    ``run(query)`` invokes the entry agent. If that agent's completion
    contains a ``HandoffRequest`` in its tool results, the coordinator
    re-invokes the target agent with the request's task (or the
    original query as fallback) plus the accumulated conversation
    history. Bounded by ``max_hops`` so accidental cycles halt.
    """

    def __init__(
        self,
        agents: Dict[str, Any],
        *,
        entry: str,
        max_hops: int = MAX_HANDOFF_HOPS,
    ):
        if entry not in agents:
            raise ValueError(
                f"entry={entry!r} not in agents ({list(agents)}). "
                "Add an entry agent to the dict."
            )
        self.agents = dict(agents)
        self.entry = entry
        self.max_hops = int(max_hops)

    @staticmethod
    def _sanitize_history_for_next_agent(
        history: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Trim the previous agent's history down to only user/assistant TEXT.

        Why: the runner's working_history contains system prompts, tool_use
        blocks with the previous agent's tool_call_ids, and function-role
        tool results. Threading all of that to the NEXT agent is a category
        error:

          - Its system prompt shouldn't leak the previous specialist's role.
          - Its LLM will see tool_call_ids from a chain it never participated
            in and OpenAI 400s ("messages with role 'function' must have a
            'name'" or "unknown tool_call_id").
          - Assistant turns with an empty text body + a tool_calls block are
            noise once the tool_call_id linkage is gone.

        What survives: user messages (the actual conversation) and assistant
        messages with non-empty text content (their prose replies). Both
        are stripped down to plain ``{"role", "content"}`` so no stale
        tool_call_id leaks through. System messages are dropped -- the
        target agent builds its own system prompt from its AgentType
        template + system_addendum.
        """
        if not history:
            return None
        clean: List[Dict[str, Any]] = []
        for m in history:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            # Drop assistant messages that are only tool-call scaffolding
            # (empty content + tool_calls block).
            content = m.get("content") or ""
            if role == "assistant" and not content and m.get("tool_calls"):
                continue
            if not content:
                continue
            clean.append({"role": role, "content": str(content)})
        return clean or None

    def _find_handoff(self, completion: AgentCompletion) -> Optional[HandoffRequest]:
        """Scan the completion's tool results for a HandoffRequest.

        Handoff tools return a ``HandoffRequest`` directly. The runner
        converts every tool result to ``str()`` when building history --
        we still need to recognize the sentinel, so the coordinator
        searches for it on ``AgentCompletion.tool_calls`` where the raw
        Python value is preserved on the ``.result`` field before
        stringification.
        """
        for tc in reversed(completion.tool_calls or []):
            # ToolCall.result is stringified for history; the actual object
            # is not preserved. So we detect by the stringified prefix
            # from HandoffRequest.__str__ -- "HANDOFF -> <target>: <task>".
            s = str(tc.result)
            if s.startswith("HANDOFF -> "):
                rest = s[len("HANDOFF -> "):]
                if ": " in rest:
                    target, task = rest.split(": ", 1)
                    if task == "(no task text)":
                        task = ""
                    return HandoffRequest(target=target, task=task)
        return None

    def run(self, query: str, chat_history: Optional[List[Dict[str, Any]]] = None) -> HandoffResult:
        """Invoke the entry agent; follow handoffs until we get a real answer
        or exceed ``max_hops``. Returns a ``HandoffResult`` bundling the
        final completion plus every handoff hop taken."""
        current_name = self.entry
        current_query = query
        history = list(chat_history) if chat_history else None
        hops: List[Dict[str, Any]] = []

        for hop in range(self.max_hops + 1):
            runner = self.agents.get(current_name)
            if runner is None:
                raise KeyError(
                    f"handoff target '{current_name}' not in agents "
                    f"({list(self.agents)})"
                )
            completion: AgentCompletion = runner.invoke(
                current_query, chat_history=history,
            )
            request = self._find_handoff(completion)
            if request is None:
                return HandoffResult(completion=completion, hops=hops)

            hops.append({
                "from": current_name, "to": request.target,
                "task": request.task, "hop": hop,
            })
            if request.target not in self.agents:
                # Unknown target -- return the completion as-is so the caller
                # can surface a useful error without silently continuing.
                completion.content = (
                    f"(handoff to unknown agent '{request.target}' -- "
                    f"no route; returning last completion)"
                )
                return HandoffResult(completion=completion, hops=hops)

            # Thread ONLY clean user/assistant text from the previous run so
            # the target agent sees prose context without inheriting stale
            # tool_call_ids or system prompts from a chain it wasn't part of.
            history = self._sanitize_history_for_next_agent(
                completion.history
            ) or history
            current_name = request.target
            current_query = request.task or query

        # Bounded -- synthesize a graceful failure completion.
        completion.content = (
            f"(handoff loop exceeded max_hops={self.max_hops}; last active "
            f"agent was '{current_name}'. Increase max_hops or review the "
            "specialists for a routing cycle.)"
        )
        return HandoffResult(completion=completion, hops=hops)

    async def arun(self, query: str, chat_history: Optional[List[Dict[str, Any]]] = None) -> HandoffResult:
        """Async sibling of ``run``. Uses each runner's ``ainvoke`` when
        available; falls back to ``invoke`` in an executor if not."""
        import asyncio
        current_name = self.entry
        current_query = query
        history = list(chat_history) if chat_history else None
        hops: List[Dict[str, Any]] = []

        for hop in range(self.max_hops + 1):
            runner = self.agents.get(current_name)
            if runner is None:
                raise KeyError(f"handoff target '{current_name}' not in agents")
            if hasattr(runner, "ainvoke"):
                completion = await runner.ainvoke(
                    current_query, chat_history=history,
                )
            else:
                loop = asyncio.get_event_loop()
                completion = await loop.run_in_executor(
                    None,
                    lambda: runner.invoke(current_query, chat_history=history),
                )
            request = self._find_handoff(completion)
            if request is None:
                return HandoffResult(completion=completion, hops=hops)
            hops.append({
                "from": current_name, "to": request.target,
                "task": request.task, "hop": hop,
            })
            if request.target not in self.agents:
                completion.content = (
                    f"(handoff to unknown agent '{request.target}' -- no route)"
                )
                return HandoffResult(completion=completion, hops=hops)
            history = completion.history or history
            current_name = request.target
            current_query = request.task or query

        completion.content = (
            f"(handoff loop exceeded max_hops={self.max_hops}; last active "
            f"agent was '{current_name}')"
        )
        return HandoffResult(completion=completion, hops=hops)
