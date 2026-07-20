# Agent-to-agent handoffs *(3.1)*

`Supervisor` decomposes top-down. `HandoffCoordinator` routes
peer-to-peer: an agent decides mid-run that another specialist should
take over.

Use when:

- Routing depends on partial results.
- Specialists chain in loops (triage → researcher → back to triage).
- You want an OpenAI-Swarm / Anthropic-agent-SDK style flow.

Contrast: [Supervisor](supervisor.md) — one planner decomposes, N
specialists execute, one synthesizer aggregates. Fixed shape known
upfront.

## Minimum viable

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    HandoffCoordinator, handoff_tool,
)

triage = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[
        handoff_tool("researcher", "Delegate deep research questions."),
        handoff_tool("writer", "Delegate final drafting."),
    ],
)

researcher = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[
        vector_search_tool(store),
        handoff_tool("writer", "Delegate final drafting."),
    ],
)

writer = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

coord = HandoffCoordinator(
    agents={"triage": triage, "researcher": researcher, "writer": writer},
    entry="triage",
    max_hops=8,
)

result = coord.run("Write a 200-word summary of MVCC.")
print(result.content)
print(result.hops)   # [{from, to, task, hop}, ...]
```

## How it works

1. `HandoffCoordinator.run(query)` invokes the `entry` agent.
2. If that agent calls a `handoff_to_<name>` tool, the tool returns a
   `HandoffRequest` sentinel.
3. The coordinator detects the request in the completion's `tool_calls`,
   re-invokes the target agent with the request's task text.
4. Chat history is **sanitized** between hops: only user/assistant text
   turns propagate. Tool_use blocks, tool_call_ids, and system prompts
   from the previous agent don't leak into the next model's call.
5. Loop repeats until an agent's completion has no handoff — that's the
   final answer.
6. Bounded by `max_hops` so cycles halt.

## The handoff tool

```python
from agentx_dev import handoff_tool

t = handoff_tool(
    target="researcher",
    description="Delegate to researcher for framework docs questions.",
    tool_name=None,   # defaults to handoff_to_researcher
)
```

The LLM sees a StructuredTool with args:

- `task: str` — what the target should handle.
- `rationale: str` — optional short explanation (not shown to the user;
  useful for tracing).

Calling it returns a `HandoffRequest(target, task, context)` sentinel.
The runner detects the sentinel and terminates its own loop cleanly.

## The result

```python
result: HandoffResult = coord.run("...")

result.completion     # AgentCompletion of the FINAL agent (the one that didn't hand off)
result.hops           # List[dict] — every hop taken
result.content        # shortcut for result.completion.content
result.tool_calls     # shortcut for result.completion.tool_calls

# result.hops shape:
# [{"from": "triage", "to": "researcher", "task": "look up MVCC", "hop": 0},
#  {"from": "researcher", "to": "writer", "task": "write summary", "hop": 1}]
```

## Async

```python
result = await coord.arun("Write a 200-word summary of MVCC.")
```

Each agent's `.ainvoke` is used if available; falls back to `.invoke`
in an executor otherwise.

## Bounded hops

```python
coord = HandoffCoordinator(agents={...}, entry="triage", max_hops=5)
```

Once the hop count exceeds `max_hops`, the coordinator returns the
last completion with `content` set to a synthesized "loop exceeded"
message. Use this to catch routing cycles (`A → B → A → B → ...`).

Default: `MAX_HANDOFF_HOPS = 8`.

## Unknown target

If an agent hands off to a name not in the coordinator's dict, the
coordinator returns immediately with:

```
"(handoff to unknown agent 'X' -- no route; returning last completion)"
```

Guard against this by validating your specialist catalog at startup:

```python
for name in coord.agents:
    for tool in coord.agents[name].tools:
        if tool.name.startswith("handoff_to_"):
            target = tool.name.removeprefix("handoff_to_")
            assert target in coord.agents, f"{name} tries to hand off to unknown {target}"
```

## Chat history propagation

`_sanitize_history_for_next_agent` (internal) keeps only clean
user/assistant text turns. Reason: passing the previous agent's full
working history would leak:

- Its system prompt (wrong role framing for the target).
- Tool_use blocks with tool_call_ids the target agent's LLM never saw.
- Function-role results the target's chat template rejects.

Result: the target agent sees the prose conversation, not the
scaffolding.

## Patterns

### Triage → specialist

```
[triage]                        (routes to the right specialist)
   |
   +-- handoff_to_researcher --> [researcher]
   +-- handoff_to_writer     --> [writer]
   +-- handoff_to_calculator --> [calculator]
```

### Escalation

```
[assistant]                     (tries to answer)
   |
   +-- handoff_to_expert  -->   [expert]   (when confidence is low)
```

### Ping-pong (bounded)

```
[researcher] <---> [reviewer]   (research + critique loop)
```

Set `max_hops` low (e.g. 4) to cap iterations.

## When NOT to use handoffs

- **Single agent, single tool** — just use `AgentRunner`.
- **Fixed multi-step pipeline** — use `Supervisor`. Cheaper on planning
  overhead when the plan shape is known.
- **Deeply hierarchical** — combine both: a top-level supervisor whose
  specialists are themselves handoff coordinators.

## Runnable demo

See `examples/v3_1_comprehensive_demo.py` — a triage → researcher →
writer pipeline with RAG + semantic memory + handoffs all wired
together.
