# Tools

A tool is a Python function the LLM can call. The framework wraps every
tool so it has:

- A **name** (what the LLM sees).
- A **description** (what tells the LLM when to use it).
- An **input schema** (what shape the arguments must have).
- Optional guards — timeout, circuit breaker, caching, observability.

Four tool classes ship in-tree:

| Class | Input shape | Sync/async |
|---|---|---|
| `StandardTool` | single positional string | sync |
| `StructuredTool` | Pydantic BaseModel with typed fields | sync |
| `AsyncStandardTool` | single positional string | async |
| `AsyncStructuredTool` | Pydantic BaseModel | async |

## StandardTool — simplest

Wrap any callable that takes one string argument:

```python
from agentx_dev import StandardTool

def get_weather(city: str) -> str:
    return f"22C, sunny in {city}"

weather = StandardTool(
    func=get_weather,
    name="get_weather",
    description="Get the current weather for a city.",
)
```

The LLM sees this tool as accepting `{"input": "<city>"}` — the
framework unwraps single-key dicts automatically so `{"city": "Paris"}`
also works.

## StructuredTool — multiple typed args

When your tool needs multiple parameters or non-string types, use
`StructuredTool` with a Pydantic schema:

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class CalculatorArgs(BaseModel):
    a: float = Field(..., description="First operand")
    b: float = Field(..., description="Second operand")
    op: str = Field(..., description="One of: add / sub / mul / div")

def calculator(a: float, b: float, op: str) -> str:
    ops = {"add": a + b, "sub": a - b, "mul": a * b, "div": a / b}
    return f"{a} {op} {b} = {ops[op]}"

calc = StructuredTool(
    func=calculator,
    args_schema=CalculatorArgs,
    name="calculator",
    description="Perform basic arithmetic (add/sub/mul/div).",
)
```

Pydantic validates the LLM's args before your function runs — a bad
`op="modulo"` raises before the call.

## Async variants

Use async when the tool is I/O-bound (HTTP, DB, disk read via aio):

```python
from agentx_dev import AsyncStandardTool, AsyncStructuredTool
import aiohttp

async def http_get(url: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.text()

fetch = AsyncStandardTool(func=http_get, name="http_get",
                          description="GET a URL and return the body.")
```

Async tools ONLY work with `AsyncAgentRunner` (or `AgentRunner` that
was called via the async path). See [Agents](agents.md).

## Descriptions matter

The LLM picks tools based on the description. Be specific:

- What does the tool do?
- What shape does the input take?
- What shape does the output take?
- When to prefer it over another tool?

Bad: `"Weather tool."`
Good: `"Get the current weather for a city. Input: city name as a string. Output: temperature in celsius + one-line summary. Prefer over web_search for weather questions."`

## Per-tool controls

Attach these attrs on the tool instance to change behavior:

```python
calc.timeout_sec = 30                    # kill dispatch after 30s
calc.circuit_breaker = CircuitBreakerConfig(
    failure_threshold=5, recovery_timeout_sec=60,
)
```

Registry-wide defaults live on `ToolRegistry`:

```python
from agentx_dev import ToolRegistry, CircuitBreakerConfig

registry = ToolRegistry(
    tools=[calc, weather],
    default_timeout_sec=30,
    circuit_breaker_config=CircuitBreakerConfig(failure_threshold=5),
)
```

The runner accepts a registry via `runner.registry = registry` after
construction (see [Production controls](../advanced/production-controls.md)).

## What runners give tools automatically

Every dispatched call gets:

1. **Duplicate-call guard** — warn at 2 repeats, refuse at 5. Prevents
   the model from spinning on the same call.
2. **Circuit breaker** — trip after N consecutive failures; short-circuit
   subsequent calls until recovery.
3. **Timeout** — kill the dispatch (sync uses `ThreadPoolExecutor`; async
   uses `asyncio.wait_for`).
4. **Cache lookup** — return prior result if `auto_cache=True` on the
   runner (or a cache is configured explicitly).
5. **Observability events** — TOOL_CALL_START / TOOL_CALL_END emitted
   if `config.observability_enabled` is on.
6. **Args signature normalization** — the framework handles OpenAI's
   `functions.` prefix and `multi_tool_use.parallel` meta-tool.

## Concurrent async dispatch

`AsyncAgentRunner` auto-registers a `batch_concurrent` tool when you
pass async tools. The LLM can call it with a batch of requests and get
them all executed via `asyncio.gather`:

```python
# LLM calls:
batch_concurrent(requests=[
    {"name": "http_get", "input": "https://api.a.com"},
    {"name": "http_get", "input": "https://api.b.com"},
    {"name": "http_get", "input": "https://api.c.com"},
])
```

For parallel dispatch WITHOUT the meta-tool, use
`bind_tools_natively=True` on the runner — the modern GPT/Claude parallel
tool-call behavior. See [Parallel tool calls](../advanced/parallel-tools.md).

## Where the built-in tools live

- **`DefaultTools`** (permission-gated, in `DefaultTools.py`) —
  `read_path`, `list_directory`, `find_files`, `grep`, `write_file`,
  `edit_file`, `delete_path`, `move_file`, `run_python`, `run_shell`.
  See [Permissions & sandbox](permissions.md).
- **`WebTools`** (opt-in) — `web_search_tool()`, `web_fetch_tool()`.
- **`vector_search_tool()`** (3.1, opt-in) — see [RAG](../advanced/rag.md).
- **`handoff_tool()`** (3.1, opt-in) — see [Handoffs](../advanced/handoffs.md).

## Authoring your own — quick recipe

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class SlackPostArgs(BaseModel):
    channel: str = Field(..., description="Channel like '#general' or user id")
    text: str = Field(..., description="Message body, markdown ok")
    thread_ts: str = Field("", description="Reply in a thread if set")

def post_to_slack(channel: str, text: str, thread_ts: str = "") -> str:
    # your slack client code
    return f"posted to {channel}"

slack = StructuredTool(
    func=post_to_slack,
    args_schema=SlackPostArgs,
    name="slack_post",
    description=(
        "Post a message to Slack. Use for one-way notifications. "
        "For interactive replies, use slack_reply instead."
    ),
)
```

For a full walkthrough with edge cases, see [Custom tools](../guides/custom-tools.md).
