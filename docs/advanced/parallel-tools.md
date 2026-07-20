# Parallel tool calls per turn *(3.1)*

Modern LLMs (Claude 3+, GPT-4o+) emit multiple `tool_use` blocks in a
single assistant response — the "parallel tool calls" feature. In text
mode the framework processes them sequentially; in native-binding mode
they dispatch concurrently.

## Turn it on

```python
from agentx_dev import AgentRunner, AgentType, Claude, StructuredTool

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[weather_tool, stock_tool, news_tool],
    bind_tools_natively=True,      # skip the parser; bind your tools directly
    parallel_tool_workers=4,       # max concurrent dispatches per turn
)
```

`bind_tools_natively=True` and `use_function_calling=True` are mutually
exclusive.

## What changes

- Your tools appear directly in the model's tool list (no
  action/action_input JSON scaffolding).
- The framework auto-registers a `respond` tool the model calls with
  its final answer to end the loop.
- When the model emits multiple `tool_use` blocks in one turn, the
  runner dispatches them on a bounded `ThreadPoolExecutor`. Sync only;
  async has always had this via `asyncio.gather`.
- The system prompt is minimal — a brief role, tool list, and
  instruction to call `respond` when done. No ReAct scaffold.

## Verify parallel dispatch

Watch the observability output:

```
[tool.call.start] fires 3 times back-to-back
[tool.call.complete] all three finish within milliseconds of each other
```

Sequential would fire complete events in staggered order. Parallel
fires them nearly simultaneously.

## Latency win

If each tool takes ~500 ms:

- Sequential: 3 × 500 ms = 1500 ms per turn
- Parallel (4 workers): ~500 ms per turn (bounded by max latency)

For I/O-bound tools (HTTP, DB, search), the win is proportional to
call count. For CPU-bound tools, Python's GIL caps the win — use
`AsyncAgentRunner` with async tools for true parallelism there.

## Sync vs async runners

| Runner | Concurrency mechanism | When to use |
|---|---|---|
| `AgentRunner` + `bind_tools_natively` | `ThreadPoolExecutor` (bounded by `parallel_tool_workers`) | Sync codebases, I/O-bound tools |
| `AsyncAgentRunner` + `bind_tools_natively` | `asyncio.gather` (unbounded within one turn) | Async codebases, true concurrency |

## Interaction with other features

- **`ToolRegistry` timeouts** — apply per tool call, not per turn. A
  3-call turn respects each tool's `timeout_sec`.
- **Circuit breakers** — a tripped breaker on one tool doesn't fail
  the others in the same turn.
- **Duplicate-call guard** — still fires. If the model calls the same
  tool 3+ times in one turn with identical args, the dup-guard warns
  or refuses per usual.
- **Prompt caching** — orthogonal. Works together on Claude.

## Common gotchas

- **GPT + no tools** — setting `parallel_tool_calls=True` on `GPT` at
  the model level breaks tool-less calls (OpenAI 400s). GPT already
  defaults to parallel when tools are present; don't set it on the
  model.
- **AgentType template fights native binding** — the framework
  automatically skips the parser scaffolding when `bind_tools_natively=True`
  and uses a minimal system prompt instead. If you need the ReAct
  scaffolding, don't turn native binding on.
- **`respond` tool** — auto-added; don't try to define your own tool
  with that name (it'd collide).

## Full example

```python
from pydantic import BaseModel, Field
from agentx_dev import AgentRunner, AgentType, Claude, StructuredTool

class CityArgs(BaseModel):
    city: str = Field(..., description="City name.")

def get_weather(city: str) -> str:
    import time; time.sleep(0.5)   # simulate HTTP
    return f"22C sunny in {city}"

def get_time(city: str) -> str:
    import time; time.sleep(0.5)
    return f"14:00 local in {city}"

def get_traffic(city: str) -> str:
    import time; time.sleep(0.5)
    return f"traffic: normal in {city}"

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[
        StructuredTool(func=get_weather, args_schema=CityArgs,
                       name="weather", description="City weather"),
        StructuredTool(func=get_time, args_schema=CityArgs,
                       name="time", description="Local time"),
        StructuredTool(func=get_traffic, args_schema=CityArgs,
                       name="traffic", description="Traffic report"),
    ],
    bind_tools_natively=True,
    parallel_tool_workers=3,
    max_iterations=3,
)

import time
t0 = time.perf_counter()
result = runner.invoke(
    "Get weather, time, and traffic for Paris. Call all three tools in parallel."
)
print(f"wall clock: {time.perf_counter() - t0:.2f}s")   # ~0.5s, not 1.5s
print(result.content)
```

## Runnable demo

`examples/v3_1_features_demo.py` — the parallel-tools section runs
three concurrent tool calls with 500 ms sleeps and shows the wall-clock
timing.
