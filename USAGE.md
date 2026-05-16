# AgentX Usage Guide (post-overhaul)

A short reference for the new and fixed behaviors. See `examples/sync_quickstart.py` and `examples/async_quickstart.py` for runnable code.

---

## What changed

### Bug fixes (silent failures that produced wrong answers)
| Bug | Before | After |
|-----|--------|-------|
| System prompt corruption on second call | Second query had first query baked into system prompt | Fresh prompt per call |
| History duplication across calls | `ChatHistory` appended every call | `working_history` is per-call |
| Structured tool double-execution | `func()` called before validation, again after | Parse → validate → call once |
| `AgentCompletion.history` always None | Hardcoded None | Returns full working history |
| Duplicate `ToolCall` class | Defined twice, second shadowed first | One definition |
| Tool description mutation | `tool.description` permanently grew on registration | Clean per-prompt formatting, tool object untouched |
| `BaseChatModel` not abstract | Empty class, no contract | `@abstractmethod Initialize` enforced |
| Dead `cleaning()` method | Broken static-like method | Removed |
| `create_stream()` calling `self.create()` | Would crash at runtime | Removed |
| ChainOfThought template | Invalid JSON, `Action Input` with space | Quoted JSON, `Action_Input` underscore |

### Architecture upgrades
- **`AsyncAgentRunner` is now truly async** — `await self.model.async_initialize()` instead of blocking sync calls
- **`Claude` chat model added** — native async via `AsyncAnthropic`, handles system message separation
- **Exponential backoff retry** — `BaseChatModel._with_retry()` wraps LLM calls (3 attempts, `0.1s → 0.2s → 0.4s`)

---

## Sync usage (`AgentRunner`)

```python
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, GPT, Claude
from agentx_dev.Tools import StandardTool, StructuredTool


# 1. Define tools
class MultiplyArgs(BaseModel):
    a: int
    b: int

def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b

mul = StructuredTool(
    func=multiply,
    args_schema=MultiplyArgs,
    name="multiply",
    description="Multiply two numbers."
)


# 2. Pick a model
model = GPT(model="gpt-4o", temperature=0.3)
# or:
# model = Claude(model="claude-sonnet-4-6", max_tokens=2048)


# 3. Build the runner
runner = AgentRunner(
    model=model,
    Agent=AgentType.ReAct,
    tools=[mul],
    max_iterations=6,
)


# 4. Reuse the runner across many calls (now safe)
r1 = runner.Initialize("What is 47 times 13?")
r2 = runner.Initialize("What about 9 times 8?")  # No longer corrupted

print(r1.content)          # "611"
print(r2.content)          # "72"
print(r2.history)          # Full conversation history (was None before)
print(r2.tool_calls)       # [ToolCall(name="multiply", ...)]
```

---

## Async usage (`AsyncAgentRunner`)

```python
import asyncio
from pydantic import BaseModel
from agentx_dev import AsyncAgentRunner, AgentType, Claude
from agentx_dev.AsyncTools import AsyncStandardTool, AsyncStructuredTool


# 1. Define async tools (I/O-bound work shines here)
class SearchArgs(BaseModel):
    query: str
    limit: int = 5

async def web_search(query: str, limit: int = 5) -> str:
    """Search the web."""
    await asyncio.sleep(0.4)  # real HTTP call goes here
    return f"Found {limit} results for '{query}'"

search = AsyncStructuredTool(
    func=web_search,
    args_schema=SearchArgs,
    name="search",
    description="Search the web."
)


async def main():
    # 2. Claude uses native async; GPT runs in a thread pool automatically
    model = Claude(model="claude-sonnet-4-6")

    runner = AsyncAgentRunner(
        model=model,
        Agent=AgentType.ReAct,
        tools=[search],
        max_iterations=6,
    )

    # 3. The LLM call no longer blocks the event loop
    result = await runner.Initialize("Search for python async tutorials")
    print(result.content)
    print(f"Steps: {result.steps}")


asyncio.run(main())
```

### Concurrent tool execution (auto-enabled)

When `AsyncAgentRunner` detects async tools in your list, it automatically adds a `batch_concurrent` tool the LLM can call to execute multiple async tools in parallel:

```python
# The LLM can issue this single tool call to run 3 lookups concurrently
{
    "action": "batch_concurrent",
    "action_input": [
        {"tool": "weather", "input": "Berlin"},
        {"tool": "weather", "input": "Tokyo"},
        {"tool": "search",  "input": "AI news"}
    ]
}
```

All three execute via `asyncio.gather()` — total latency = slowest single call, not the sum.

---

## Mixing sync + async tools

You can pass both in the same `AsyncAgentRunner`:

```python
runner = AsyncAgentRunner(
    model=model,
    Agent=AgentType.ReAct,
    tools=[
        sync_calculator_tool,    # StandardTool / StructuredTool
        async_search_tool,       # AsyncStandardTool / AsyncStructuredTool
        async_weather_tool,
    ],
)
```

The runner dispatches each tool with the right execution model.

---

## Using Claude

```python
from agentx_dev import Claude

# Default: latest Sonnet
model = Claude()

# Or specify
model = Claude(
    model="claude-sonnet-4-6",   # also: claude-opus-4-7, claude-haiku-4-5-20251001
    max_tokens=4096,
    temperature=1.0,
    timeout=60.0,
    max_retries=3,
)
```

`ANTHROPIC_API_KEY` env var is read automatically. System messages from the runner are extracted and passed via Anthropic's `system` parameter (the API requires this separation).

---

## Retry behavior

LLM calls are wrapped in `BaseChatModel._with_retry()`:
- 3 attempts total
- Exponential backoff: 0.1s, 0.2s, 0.4s
- Re-raises the final exception after exhausting retries
- Logs a warning before each retry

This catches transient `ConnectionError`, `TimeoutError`, and other network blips. The OpenAI SDK's built-in retries (for rate limits) still apply on top.

---

## What's still on the roadmap

- Native OpenAI tool-calling API (replace JSON text parsing)
- Multi-agent supervisor / orchestrator
- Semantic memory with vector retrieval
- Planning agent (plan-then-execute pattern)
- Streaming responses through the runner (the `Streaming` module exists but isn't wired through `Initialize`)
