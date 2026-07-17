"""
Async AsyncAgentRunner quickstart — concurrent tool execution + non-blocking LLM calls.

The LLM call now uses `await model.async_initialize()` so the event loop
is no longer blocked. Claude uses native AsyncAnthropic; GPT runs the sync
client in a thread pool automatically.

Run:
    export OPENAI_API_KEY=sk-...
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/async_quickstart.py
"""

import asyncio
from pydantic import BaseModel
from agentx_dev import AsyncAgentRunner, AgentType, GPT, Claude
from agentx_dev.AsyncTools import AsyncStandardTool, AsyncStructuredTool


# --- Define async tools (I/O-bound work) ---

class SearchArgs(BaseModel):
    query: str
    limit: int = 5


async def web_search(query: str, limit: int = 5) -> str:
    """Async web search (simulated)."""
    await asyncio.sleep(0.4)  # simulated network call
    return f"Found {limit} results for '{query}'"


async def fetch_weather(city: str) -> str:
    """Async weather fetch (simulated)."""
    await asyncio.sleep(0.3)
    return f"{city}: 19°C, light rain"


search_tool = AsyncStructuredTool(
    func=web_search,
    args_schema=SearchArgs,
    name="search",
    description="Search the web for information.",
)

weather_tool = AsyncStandardTool(
    func=fetch_weather,
    name="weather",
    description="Get current weather for a city.",
)


async def main():
    # Pick a model — Claude uses native async, GPT runs in a thread pool
    model = Claude(model="claude-sonnet-4-6", max_tokens=2048)
    # model = GPT(model="gpt-4o")

    runner = AsyncAgentRunner(
        model=model,
        Agent=AgentType.ReAct,
        tools=[search_tool, weather_tool],
        max_iterations=6,
    )

    # First call
    r1 = await runner.Initialize(
        "What's the weather in Berlin and search for recent AI news?"
    )
    print("Answer 1:", r1.content)
    print("Tool calls:", [tc.name for tc in r1.tool_calls])

    # Reusing the same runner now works correctly
    r2 = await runner.Initialize("Search for python tutorials")
    print("\nAnswer 2:", r2.content)

    # Returned history is no longer None
    print(f"\nHistory length: {len(r2.history)} messages")


if __name__ == "__main__":
    asyncio.run(main())
