"""
Example: Concurrent Tool Execution vs Sequential Agent Loop

This demonstrates:
1. Sequential ReAct agent loop (standard)
2. Concurrent batch tool execution (for speed)
3. Hybrid approach (best of both)
"""

import asyncio
import time
from agentx_dev import AsyncStandardTool, execute_tools_concurrently


# Simulated async tools
async def fetch_weather(location: str) -> str:
    """Simulate weather API call."""
    print(f"  [API] Fetching weather for {location}...")
    await asyncio.sleep(1)  # Simulate API delay
    return f"Weather in {location}: Sunny, 72°F"


async def fetch_news(topic: str) -> str:
    """Simulate news API call."""
    print(f"  [API] Fetching news about {topic}...")
    await asyncio.sleep(1)
    return f"Latest news on {topic}: Article 1, Article 2"


async def fetch_stock(symbol: str) -> str:
    """Simulate stock API call."""
    print(f"  [API] Fetching stock price for {symbol}...")
    await asyncio.sleep(1)
    return f"{symbol}: $150.25 (+2.3%)"


# =============================================================================
# Example 1: Sequential Execution (How AsyncAgentRunner works by default)
# =============================================================================

async def example_sequential():
    """Sequential tool execution - standard ReAct pattern."""
    print("="*70)
    print("Example 1: Sequential Execution (Standard ReAct Pattern)")
    print("="*70 + "\n")

    print("Calling 3 tools sequentially...")
    print()

    start = time.time()

    # Call each tool one after another (like agent loop does)
    result1 = await fetch_weather("NYC")
    print(f"Result 1: {result1}\n")

    result2 = await fetch_news("AI")
    print(f"Result 2: {result2}\n")

    result3 = await fetch_stock("AAPL")
    print(f"Result 3: {result3}\n")

    duration = time.time() - start
    print(f"Total time: {duration:.2f} seconds")
    print("(Each call waits for the previous one to complete)")
    print()


# =============================================================================
# Example 2: Concurrent Execution (Using execute_tools_concurrently)
# =============================================================================

async def example_concurrent():
    """Concurrent tool execution - all at once!"""
    print("="*70)
    print("Example 2: Concurrent Execution (Parallel)")
    print("="*70 + "\n")

    # Create tools
    weather_tool = AsyncStandardTool(func=fetch_weather, name="weather")
    news_tool = AsyncStandardTool(func=fetch_news, name="news")
    stock_tool = AsyncStandardTool(func=fetch_stock, name="stock")

    # Define what to execute
    tools_to_run = [
        {'tool': weather_tool, 'args': {'input': 'NYC'}},
        {'tool': news_tool, 'args': {'input': 'AI'}},
        {'tool': stock_tool, 'args': {'input': 'AAPL'}}
    ]

    print("Calling 3 tools concurrently...")
    print()

    start = time.time()

    # Execute all at once!
    results = await execute_tools_concurrently(tools_to_run)

    duration = time.time() - start

    print()
    for i, result in enumerate(results, 1):
        print(f"Result {i}: {result}")

    print()
    print(f"Total time: {duration:.2f} seconds")
    print("(All calls run in parallel - 3x faster!)")
    print()


# =============================================================================
# Example 3: Batch Multiple Similar Calls
# =============================================================================

async def example_batch_weather():
    """Fetch weather for multiple cities concurrently."""
    print("="*70)
    print("Example 3: Batch Processing (Multiple Similar Calls)")
    print("="*70 + "\n")

    weather_tool = AsyncStandardTool(func=fetch_weather, name="weather")

    cities = ["NYC", "LA", "Chicago", "Miami", "Seattle"]

    # Create tool calls for all cities
    tools_to_run = [
        {'tool': weather_tool, 'args': {'input': city}}
        for city in cities
    ]

    print(f"Fetching weather for {len(cities)} cities concurrently...")
    print()

    start = time.time()
    results = await execute_tools_concurrently(tools_to_run)
    duration = time.time() - start

    print()
    print("Results:")
    for city, weather in zip(cities, results):
        print(f"  {city}: {weather}")

    print()
    print(f"Total time: {duration:.2f} seconds")
    print(f"(Would take {len(cities):.0f} seconds sequentially!)")
    print()


# =============================================================================
# Example 4: Why Sequential is Sometimes Needed
# =============================================================================

async def example_dependent_steps():
    """Show why some operations must be sequential."""
    print("="*70)
    print("Example 4: Dependent Steps (Must Be Sequential)")
    print("="*70 + "\n")

    print("Scenario: Book a flight (each step depends on previous)")
    print()

    # Step 1: Check available flights
    print("Step 1: Checking available flights...")
    await asyncio.sleep(0.5)
    available_flight = "AA123 at 3pm"
    print(f"  Found: {available_flight}\n")

    # Step 2: Check calendar (depends on knowing the flight time)
    print("Step 2: Checking calendar for 3pm...")
    await asyncio.sleep(0.5)
    conflict = "Meeting at 3pm"
    print(f"  Conflict: {conflict}\n")

    # Step 3: Check next flight (depends on knowing there's a conflict)
    print("Step 3: Checking next available flight...")
    await asyncio.sleep(0.5)
    next_flight = "AA456 at 5pm"
    print(f"  Found: {next_flight}\n")

    # Step 4: Book it (depends on finding available flight)
    print("Step 4: Booking flight AA456...")
    await asyncio.sleep(0.5)
    print(f"  Booked!\n")

    print("These steps CANNOT run concurrently - each depends on the previous!")
    print("This is why the AsyncAgentRunner loop is sequential.")
    print()


# =============================================================================
# Example 5: Hybrid Approach (Best of Both)
# =============================================================================

async def example_hybrid():
    """Combine sequential reasoning with concurrent data fetching."""
    print("="*70)
    print("Example 5: Hybrid Approach (Sequential + Concurrent)")
    print("="*70 + "\n")

    print("Scenario: Research assistant gathering market data")
    print()

    # Phase 1: Sequential reasoning
    print("Phase 1: Determine what data we need (sequential)")
    print("  Agent thinking: Need weather, news, and stock data...")
    await asyncio.sleep(0.3)
    print("  Decision: Fetch all three in parallel!\n")

    # Phase 2: Concurrent data gathering
    print("Phase 2: Fetch all data concurrently")

    weather_tool = AsyncStandardTool(func=fetch_weather, name="weather")
    news_tool = AsyncStandardTool(func=fetch_news, name="news")
    stock_tool = AsyncStandardTool(func=fetch_stock, name="stock")

    tools_to_run = [
        {'tool': weather_tool, 'args': {'input': 'NYC'}},
        {'tool': news_tool, 'args': {'input': 'Tech'}},
        {'tool': stock_tool, 'args': {'input': 'GOOGL'}}
    ]

    start = time.time()
    results = await execute_tools_concurrently(tools_to_run)
    duration = time.time() - start

    print()
    print(f"  All data fetched in {duration:.2f}s\n")

    # Phase 3: Sequential synthesis
    print("Phase 3: Synthesize results (sequential)")
    print("  Agent analyzing data...")
    await asyncio.sleep(0.3)
    print("  Agent: 'Based on the data, here's my recommendation...'\n")

    print("This hybrid approach combines:")
    print("  - Sequential reasoning (agent decides what to do)")
    print("  - Concurrent execution (fetch data fast)")
    print("  - Sequential synthesis (agent makes sense of results)")
    print()


# =============================================================================
# Main
# =============================================================================

async def main():
    print("\n" + "="*70)
    print("AgentX: Concurrent vs Sequential Tool Execution")
    print("="*70 + "\n")

    # Run all examples
    await example_sequential()
    await asyncio.sleep(0.5)

    await example_concurrent()
    await asyncio.sleep(0.5)

    await example_batch_weather()
    await asyncio.sleep(0.5)

    await example_dependent_steps()
    await asyncio.sleep(0.5)

    await example_hybrid()

    print("="*70)
    print("Key Takeaways:")
    print("="*70)
    print()
    print("1. AsyncAgentRunner uses SEQUENTIAL execution (ReAct pattern)")
    print("   - Each step depends on previous results")
    print("   - Agent reasons between actions")
    print()
    print("2. Use execute_tools_concurrently() for PARALLEL execution")
    print("   - When you know what to call ahead of time")
    print("   - For independent operations (3x+ faster!)")
    print()
    print("3. Best practice: HYBRID approach")
    print("   - Sequential reasoning (agent decides)")
    print("   - Concurrent data fetching (speed)")
    print("   - Sequential synthesis (agent analyzes)")
    print()
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
