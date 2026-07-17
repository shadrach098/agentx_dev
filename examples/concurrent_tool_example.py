"""
Simple Example: Add Concurrent Execution as a Custom Tool

Just copy this tool and add it to your agent - it will automatically
execute multiple async operations concurrently!
"""

import asyncio
import json
from agentx_dev import AsyncAgentRunner, AgentType, GPT
from agentx_dev import AsyncStandardTool, AsyncStructuredTool
from pydantic import BaseModel


# =============================================================================
# Your Async Tools (these will run concurrently!)
# =============================================================================

async def fetch_weather(location: str) -> str:
    """Fetch weather for a location."""
    print(f"  [WEATHER API] Fetching {location}...")
    await asyncio.sleep(1)  # Simulate API delay
    return f"Weather in {location}: Sunny, 72°F"


async def fetch_news(topic: str) -> str:
    """Fetch news about a topic."""
    print(f"  [NEWS API] Fetching news on {topic}...")
    await asyncio.sleep(1)
    return f"Latest news on {topic}: Breaking stories!"


async def fetch_stock(symbol: str) -> str:
    """Fetch stock price."""
    print(f"  [STOCK API] Fetching {symbol}...")
    await asyncio.sleep(1)
    return f"{symbol}: $150.25 (+2.3%)"


# =============================================================================
# CONCURRENT BATCH TOOL - Just add this to your agent!
# =============================================================================

class BatchRequest(BaseModel):
    """Schema for batch concurrent requests."""
    requests: list  # List of {"tool": "weather", "input": "NYC"}


async def batch_concurrent_calls(requests: list) -> str:
    """
    Execute multiple tool calls concurrently.

    This tool automatically runs multiple operations in parallel!

    Args:
        requests: List of dicts like [{"tool": "weather", "input": "NYC"}, ...]

    Returns:
        JSON string with all results
    """
    print(f"\n[BATCH] Executing {len(requests)} calls concurrently...")

    # Map tool names to functions
    tool_map = {
        "weather": fetch_weather,
        "news": fetch_news,
        "stock": fetch_stock
    }

    # Create tasks for all requests
    tasks = []
    for req in requests:
        tool_name = req.get("tool")
        tool_input = req.get("input")

        if tool_name in tool_map:
            tasks.append(tool_map[tool_name](tool_input))
        else:
            tasks.append(asyncio.sleep(0))  # Placeholder for unknown tools

    # Execute all concurrently!
    results = await asyncio.gather(*tasks)

    # Format results
    output = {}
    for i, req in enumerate(requests):
        key = f"{req.get('tool')}_{req.get('input')}"
        output[key] = results[i]

    print(f"[BATCH] All {len(requests)} calls completed!\n")

    return json.dumps(output, indent=2)


# =============================================================================
# Usage Example
# =============================================================================

async def main():
    print("="*70)
    print("Concurrent Batch Tool Example")
    print("="*70 + "\n")

    # Create the concurrent batch tool
    batch_tool = AsyncStructuredTool(
        func=batch_concurrent_calls,
        args_schema=BatchRequest,
        name="batch_concurrent",
        description=(
            "Execute multiple tool calls concurrently for faster results. "
            "Input should be a list of requests like: "
            '[{"tool": "weather", "input": "NYC"}, {"tool": "news", "input": "AI"}]'
        )
    )

    # Also create individual tools (for single calls)
    weather_tool = AsyncStandardTool(
        func=fetch_weather,
        name="weather",
        description="Get weather for a location"
    )

    news_tool = AsyncStandardTool(
        func=fetch_news,
        name="news",
        description="Get news about a topic"
    )

    stock_tool = AsyncStandardTool(
        func=fetch_stock,
        name="stock",
        description="Get stock price for a symbol"
    )

    print("Created tools:")
    print("  - weather (individual)")
    print("  - news (individual)")
    print("  - stock (individual)")
    print("  - batch_concurrent (runs multiple in parallel!)")
    print()

    # Test the batch tool directly
    print("="*70)
    print("Test 1: Calling batch tool directly")
    print("="*70 + "\n")

    requests = [
        {"tool": "weather", "input": "NYC"},
        {"tool": "weather", "input": "LA"},
        {"tool": "news", "input": "AI"},
        {"tool": "stock", "input": "AAPL"}
    ]

    import time
    start = time.time()
    result = await batch_concurrent_calls(requests)
    duration = time.time() - start

    print("Results:")
    print(result)
    print()
    print(f"Time: {duration:.2f}s (would be {len(requests):.0f}s if sequential!)")
    print()

    # Show how to use with agent
    print("="*70)
    print("Test 2: How to use with AsyncAgentRunner")
    print("="*70 + "\n")

    print("""
# Create agent with all tools including batch tool
agent = AsyncAgentRunner(
    model=GPT(model="gpt-4", api_key="your-key"),
    Agent=AgentType.ReAct,
    tools=[
        weather_tool,
        news_tool,
        stock_tool,
        batch_tool  # ← The concurrent batch tool!
    ]
)

# The agent can now use batch_concurrent for multiple calls!
response = await agent.Initialize(
    "Get weather for NYC and LA, plus AI news"
)

# Agent will automatically use batch_concurrent tool to fetch all data
# concurrently instead of making 3 separate sequential calls!
""")

    print("\nThe agent's prompt should mention:")
    print('  "Use batch_concurrent when you need to call multiple tools at once"')
    print()


# =============================================================================
# Even Simpler: Auto-Detect Tool
# =============================================================================

async def smart_batch_tool(query: str) -> str:
    """
    SMART BATCH TOOL - Automatically detects and executes multiple calls.

    Just pass a query like: "Get weather for NYC, LA, and Chicago"
    It will automatically:
    1. Parse the query
    2. Detect multiple similar requests
    3. Execute them concurrently!
    """
    print(f"\n[SMART BATCH] Processing query: {query}")

    # Simple parsing (you can make this smarter)
    if "weather" in query.lower():
        # Extract cities (simplified - use LLM for real parsing)
        import re
        cities = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', query)

        if len(cities) > 1:
            print(f"[SMART BATCH] Detected {len(cities)} weather requests")
            print(f"[SMART BATCH] Executing concurrently...")

            tasks = [fetch_weather(city) for city in cities]
            results = await asyncio.gather(*tasks)

            output = "\n".join([
                f"{city}: {result}"
                for city, result in zip(cities, results)
            ])

            print(f"[SMART BATCH] Done!\n")
            return output

    # Fallback for single query
    return "Please specify multiple items for concurrent execution"


async def example_smart_batch():
    """Example using the smart batch tool."""
    print("\n" + "="*70)
    print("Smart Batch Tool Example (Auto-Detects Concurrent Calls)")
    print("="*70 + "\n")

    smart_tool = AsyncStandardTool(
        func=smart_batch_tool,
        name="smart_batch",
        description="Intelligently handles queries that need multiple API calls"
    )

    # Test it
    query = "Get weather for NYC, LA, Chicago, and Miami"

    import time
    start = time.time()
    result = await smart_batch_tool(query)
    duration = time.time() - start

    print("Results:")
    print(result)
    print()
    print(f"Time: {duration:.2f}s (4 calls in parallel!)")
    print()


# =============================================================================
# Run Examples
# =============================================================================

if __name__ == "__main__":
    print("\n" + "="*70)
    print("Concurrent Tool Examples for AgentX")
    print("="*70 + "\n")

    async def run_all():
        await main()
        await example_smart_batch()

        print("="*70)
        print("Summary: Three Ways to Add Concurrent Execution")
        print("="*70)
        print()
        print("1. BATCH TOOL (Explicit)")
        print("   - Define batch_concurrent_calls() tool")
        print("   - Agent calls it with multiple requests")
        print("   - Executes all concurrently")
        print()
        print("2. SMART TOOL (Auto-Detect)")
        print("   - Define smart_batch_tool() that parses queries")
        print("   - Automatically detects multiple similar requests")
        print("   - Executes concurrently without agent knowing")
        print()
        print("3. MANUAL (Full Control)")
        print("   - Use execute_tools_concurrently() directly")
        print("   - You control exactly what runs when")
        print()
        print("Choose based on your needs:")
        print("  - Need agent control? → Batch Tool")
        print("  - Want automatic? → Smart Tool")
        print("  - Need precision? → Manual")
        print()
        print("="*70 + "\n")

    asyncio.run(run_all())
