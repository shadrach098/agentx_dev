"""
Integration test: batch_concurrent tool auto-add still works after the overhaul.

Drives AsyncAgentRunner with a scripted mock model that issues a single
batch_concurrent call against 3 async tools. Confirms:
  1. batch_concurrent is auto-registered when async tools are present
  2. The LLM can invoke it with a list of {tool, input} dicts
  3. All 3 sub-tool calls execute CONCURRENTLY via asyncio.gather (not serially)
  4. Results come back keyed by tool+input
"""

import asyncio
import json
import time
import pytest
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.AsyncTools import AsyncStandardTool
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner


class ScriptedModel(BaseChatModel):
    def __init__(self, responses):
        self._iter = iter(responses)

    def Initialize(self, messages) -> str:
        return next(self._iter)


# Three async tools, each sleeps 0.4s. Sequential = ~1.2s, concurrent = ~0.4s.
async def fetch_weather(city: str) -> str:
    """Async weather lookup."""
    await asyncio.sleep(0.4)
    return f"{city}: 22°C"


async def fetch_stock(ticker: str) -> str:
    """Async stock lookup."""
    await asyncio.sleep(0.4)
    return f"{ticker}: $142.50"


async def fetch_news(topic: str) -> str:
    """Async news lookup."""
    await asyncio.sleep(0.4)
    return f"News on {topic}: lorem ipsum"


def test_batch_concurrent_auto_added_and_runs_in_parallel():
    """End-to-end: batch_concurrent is auto-added AND executes async tools concurrently."""
    asyncio.run(_run_batch_concurrent_check())


async def _run_batch_concurrent_check():
    weather = AsyncStandardTool(func=fetch_weather, name="weather", description="Async weather.")
    stock   = AsyncStandardTool(func=fetch_stock,   name="stock",   description="Async stock.")
    news    = AsyncStandardTool(func=fetch_news,    name="news",    description="Async news.")

    # Scripted LLM responses:
    # Turn 1: call batch_concurrent with 3 lookups in parallel
    # Turn 2: produce final answer using the batch results
    scripted = [
        json.dumps({
            "Thought": "I need three things. Use batch_concurrent to fetch them in parallel.",
            "action": "batch_concurrent",
            "action_input": [
                {"tool": "weather", "input": "Tokyo"},
                {"tool": "stock",   "input": "AAPL"},
                {"tool": "news",    "input": "AI"},
            ],
        }),
        json.dumps({
            "Thought": "I have all three results. Compose the final answer.",
            "action": "Final_Answer",
            "action_input": "Tokyo is 22C, AAPL is $142.50, and there's news on AI.",
        }),
    ]

    runner = AsyncAgentRunner(
        model=ScriptedModel(scripted),
        Agent=AgentType.ReAct,
        tools=[weather, stock, news],
        max_iterations=4,
        auto_cache=False,
    )

    # CHECK 1: batch_concurrent was auto-added
    tool_names = [t.name for t in runner.tools]
    print(f"Registered tools: {tool_names}")
    assert "batch_concurrent" in tool_names, "batch_concurrent should be auto-added"
    print("CHECK 1 PASS: batch_concurrent auto-added\n")

    # CHECK 2 + 3: run end-to-end and time it
    start = time.perf_counter()
    result = await runner.Initialize("Give me Tokyo weather, AAPL stock, and AI news")
    elapsed = time.perf_counter() - start

    print(f"\nFinal answer: {result.content}")
    print(f"Elapsed: {elapsed:.2f}s (sequential would be ~1.2s, concurrent ~0.4s)")

    # CHECK 2: tool call was issued
    assert any(tc.name == "batch_concurrent" for tc in result.tool_calls), \
        "batch_concurrent should appear in tool_calls"
    print("\nCHECK 2 PASS: batch_concurrent invoked by the agent")

    # CHECK 3: concurrency — must be well under 1.0s (3x 0.4s serially would be ~1.2s)
    assert elapsed < 1.0, f"Expected <1s for concurrent exec, got {elapsed:.2f}s"
    print(f"CHECK 3 PASS: ran concurrently in {elapsed:.2f}s (not serially)")

    # CHECK 4: result contents
    batch_call = next(tc for tc in result.tool_calls if tc.name == "batch_concurrent")
    print(f"\nBatch result payload:\n{batch_call.result}")
    payload = json.loads(batch_call.result)
    assert "weather_Tokyo" in payload
    assert "stock_AAPL" in payload
    assert "news_AI" in payload
    print("CHECK 4 PASS: all three results keyed correctly\n")

    print("ALL CHECKS PASSED — batch_concurrent still works end-to-end")


if __name__ == "__main__":
    asyncio.run(_run_batch_concurrent_check())
