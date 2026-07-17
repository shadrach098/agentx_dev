"""
Complete Example: All AgentX Enhanced Features Together

This example demonstrates how to use all enhanced features together
in a real-world scenario: A multi-turn conversation agent with async tools,
caching, observability, and memory management.
"""

import asyncio
import time
from agentx_dev import (
    # Core
    AsyncAgentRunner, AgentType, GPT,

    # Async Tools
    AsyncStandardTool, AsyncStructuredTool,

    # Observability
    observability, ConsoleHook, MetricsHook, FileHook,

    # Memory
    TokenLimitedMemory,

    # Caching
    InMemoryCache, cached_tool,
)
from pydantic import BaseModel


# ============================================================================
# SETUP: Tools with Caching
# ============================================================================

# Create cache for expensive operations
cache = InMemoryCache(default_ttl=300)  # 5 minutes


@cached_tool(cache, ttl=60)
async def fetch_weather_data(location: str) -> str:
    """
    Fetch weather data (simulated expensive API call).
    Results are cached for 60 seconds.
    """
    print(f"  [API] Fetching weather for {location}...")
    await asyncio.sleep(1)  # Simulate API delay

    # Simulate weather data
    weather_data = {
        "New York": "Sunny, 72°F",
        "Los Angeles": "Partly cloudy, 68°F",
        "Chicago": "Rainy, 55°F",
        "Miami": "Hot and humid, 85°F",
    }

    return weather_data.get(location, "Weather data not available")


class SearchParams(BaseModel):
    query: str
    limit: int = 5


@cached_tool(cache, ttl=120)
async def web_search(query: str, limit: int = 5) -> str:
    """
    Perform web search (simulated).
    Results are cached for 2 minutes.
    """
    print(f"  [API] Searching for '{query}' (limit: {limit})...")
    await asyncio.sleep(0.5)  # Simulate API delay

    return f"Found {limit} results for '{query}': [Result 1, Result 2, ...]"


# Non-cached tools (deterministic operations)
async def calculator(expression: str) -> str:
    """Calculate mathematical expressions."""
    try:
        # Simple eval (use sympy or safe eval in production)
        result = eval(expression)
        return f"Result: {result}"
    except Exception as e:
        return f"Error: {str(e)}"


# ============================================================================
# SETUP: Observability
# ============================================================================

def setup_observability():
    """Configure observability hooks."""
    # Console output with colors
    console_hook = ConsoleHook(verbose=False, color=True)
    observability.add_hook(console_hook)

    # Collect performance metrics
    metrics_hook = MetricsHook()
    observability.add_hook(metrics_hook)

    # Log to file
    file_hook = FileHook(filepath="agent_session.jsonl", append=False)
    observability.add_hook(file_hook)

    return metrics_hook


# ============================================================================
# SETUP: Memory Management
# ============================================================================

def create_memory():
    """Create memory with token limits."""
    # Keep conversation within 4000 tokens
    return TokenLimitedMemory(max_tokens=4000, preserve_system=True)


# ============================================================================
# MAIN: Multi-Turn Conversation Agent
# ============================================================================

async def main():
    print("\n" + "="*70)
    print("AgentX Complete Example: All Enhanced Features")
    print("="*70 + "\n")

    # Setup observability
    print("Setting up observability...")
    metrics_hook = setup_observability()
    print("✓ Observability configured\n")

    # Setup memory
    print("Initializing memory management...")
    memory = create_memory()
    print("✓ Memory initialized (max 4000 tokens)\n")

    # Create async tools
    print("Creating async tools with caching...")

    weather_tool = AsyncStandardTool(
        func=fetch_weather_data,
        name="weather",
        description="Get current weather for a location (cached for 60s)"
    )

    search_tool = AsyncStructuredTool(
        func=web_search,
        args_schema=SearchParams,
        name="search",
        description="Search the web for information (cached for 120s)"
    )

    calc_tool = AsyncStandardTool(
        func=calculator,
        name="calculator",
        description="Calculate mathematical expressions"
    )

    print("✓ 3 async tools created\n")

    # Create agent
    print("Note: To run the actual agent, you need to set OPENAI_API_KEY")
    print("The following code demonstrates the setup:\n")

    # Uncomment to run with actual OpenAI API
    # chat_model = GPT(model="gpt-4", api_key="your-key-here")
    #
    # agent = AsyncAgentRunner(
    #     model=chat_model,
    #     Agent=AgentType.ReAct,
    #     tools=[weather_tool, search_tool, calc_tool],
    #     max_iterations=5
    # )

    print("="*70)
    print("SIMULATED MULTI-TURN CONVERSATION")
    print("="*70 + "\n")

    # Simulate multiple conversation turns
    queries = [
        "What's the weather in New York?",
        "Search for AI news",
        "What's 25 * 43?",
        "What's the weather in New York again?",  # Should use cache
        "Search for AI news again",  # Should use cache
    ]

    for i, query in enumerate(queries, 1):
        print(f"\n--- Turn {i} ---")
        print(f"User: {query}\n")

        # Simulate tool execution to demonstrate caching
        if "weather" in query.lower():
            location = "New York" if "New York" in query else "Chicago"
            start = time.time()
            result = await fetch_weather_data(location)
            duration = time.time() - start
            print(f"Weather Tool: {result}")
            print(f"Time: {duration:.3f}s {'(cached!)' if duration < 0.1 else ''}\n")

            # Add to memory
            memory.add_message("user", query)
            memory.add_message("assistant", f"The weather in {location} is: {result}")

        elif "search" in query.lower():
            start = time.time()
            result = await web_search("AI news", limit=5)
            duration = time.time() - start
            print(f"Search Tool: {result}")
            print(f"Time: {duration:.3f}s {'(cached!)' if duration < 0.1 else ''}\n")

            memory.add_message("user", query)
            memory.add_message("assistant", result)

        elif "calculate" in query.lower() or "*" in query:
            result = await calculator("25 * 43")
            print(f"Calculator Tool: {result}\n")

            memory.add_message("user", query)
            memory.add_message("assistant", result)

        # Show memory stats
        print(f"Memory: {memory.get_total_tokens()} tokens, {len(memory.get_messages())} messages")

    # ========================================================================
    # Show Results
    # ========================================================================

    print("\n" + "="*70)
    print("SESSION SUMMARY")
    print("="*70 + "\n")

    # Cache statistics
    print("Cache Performance:")
    cache_stats = cache.get_stats()
    print(f"  Total entries: {cache_stats['total_entries']}")
    print(f"  Active entries: {cache_stats['active_entries']}")
    print("\nNote: Repeated queries were served from cache (instant response)\n")

    # Memory statistics
    print("Memory Management:")
    print(f"  Total tokens: {memory.get_total_tokens()}")
    print(f"  Total messages: {len(memory.get_messages())}")
    print(f"  Token limit: 4000")
    print("  Status: ✓ Within limits\n")

    # Observability metrics
    print("Performance Metrics:")
    metrics_summary = metrics_hook.get_summary()

    if metrics_summary['event_counts']:
        print("  Event Counts:")
        for event_type, count in metrics_summary['event_counts'].items():
            print(f"    {event_type}: {count}")

    if metrics_summary['duration_stats']:
        print("\n  Average Durations:")
        for event_type, stats in metrics_summary['duration_stats'].items():
            print(f"    {event_type}: {stats['avg_ms']:.2f}ms")

    print("\n  Event log: agent_session.jsonl")

    # ========================================================================
    # Advanced Usage Examples
    # ========================================================================

    print("\n" + "="*70)
    print("ADVANCED FEATURES DEMONSTRATION")
    print("="*70 + "\n")

    print("1. Cache Management:")
    print(f"   - Check if key exists: cache.has('weather:New York') = {cache.has('weather:New York')}")
    print(f"   - Get cached value: {cache.get('weather:New York')}")
    print(f"   - Delete entry: cache.delete('key')")
    print(f"   - Clear all: cache.clear()")

    print("\n2. Memory Strategies:")
    print("   Current: TokenLimitedMemory(max_tokens=4000)")
    print("   Alternatives:")
    print("   - SlidingWindowMemory(max_messages=10) - Keep last N messages")
    print("   - ImportanceBasedMemory() - Keep important messages")
    print("   - SummaryMemory(summarizer) - Compress old messages")

    print("\n3. Observability:")
    print("   Active hooks: ConsoleHook, MetricsHook, FileHook")
    print("   Custom hooks: CallbackHook(your_function)")
    print("   Decorators: @track_tool_call, @track_async_tool_call")

    print("\n4. Async Execution:")
    print("   - AsyncStandardTool for simple async functions")
    print("   - AsyncStructuredTool for Pydantic-validated args")
    print("   - execute_tools_concurrently() for parallel execution")

    # ========================================================================
    # Usage with Real Agent (commented out)
    # ========================================================================

    print("\n" + "="*70)
    print("TO USE WITH REAL AGENT")
    print("="*70 + "\n")

    print("""
# 1. Set your OpenAI API key
import os
os.environ['OPENAI_API_KEY'] = 'your-key-here'

# 2. Create agent
from agentx_dev import AsyncAgentRunner, AgentType, GPT

chat_model = GPT(model="gpt-4")
agent = AsyncAgentRunner(
    model=chat_model,
    Agent=AgentType.ReAct,
    tools=[weather_tool, search_tool, calc_tool],
    max_iterations=5
)

# 3. Run multi-turn conversation
memory = TokenLimitedMemory(max_tokens=4000)

for query in user_queries:
    response = await agent.Initialize(
        query,
        ChatHistory=memory.get_messages()
    )

    # Update memory
    memory.add_message("user", query)
    memory.add_message("assistant", response.content)

    print(f"Agent: {response.content}")
    """)

    print("\n" + "="*70)
    print("Example completed!")
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
