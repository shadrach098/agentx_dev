"""
Example: Async Tool Execution with AgentX

This example demonstrates how to use async tools for concurrent execution,
which is particularly useful for I/O-bound operations like API calls.
"""

import asyncio
from agentx_dev import (
    AsyncAgentRunner, AsyncStandardTool, AsyncStructuredTool,
    AgentType, GPT, execute_tools_concurrently
)
from pydantic import BaseModel


# Example 1: Async Standard Tool
async def fetch_weather(location: str) -> str:
    """Simulate fetching weather from an API."""
    print(f"Fetching weather for {location}...")
    await asyncio.sleep(1)  # Simulate API delay
    return f"Weather in {location}: Sunny, 72°F"


# Example 2: Async Structured Tool
class SearchArgs(BaseModel):
    query: str
    limit: int = 10


async def web_search(query: str, limit: int = 10) -> str:
    """Simulate web search API call."""
    print(f"Searching for '{query}' (limit: {limit})...")
    await asyncio.sleep(0.5)  # Simulate API delay
    return f"Found {limit} results for query: '{query}'"


# Example 3: Multiple concurrent tool calls
async def concurrent_execution_demo():
    """Demonstrate executing multiple tools concurrently."""
    print("=== Concurrent Tool Execution Demo ===\n")

    # Create async tools
    weather_tool = AsyncStandardTool(
        func=fetch_weather,
        name="weather",
        description="Get current weather for a location"
    )

    search_tool = AsyncStructuredTool(
        func=web_search,
        args_schema=SearchArgs,
        name="search",
        description="Search the web for information"
    )

    # Define tools to execute concurrently
    tools_to_run = [
        {'tool': weather_tool, 'args': {'input': 'New York'}},
        {'tool': weather_tool, 'args': {'input': 'Los Angeles'}},
        {'tool': weather_tool, 'args': {'input': 'Chicago'}},
        {'tool': search_tool, 'args': {'query': 'AI news', 'limit': 5}},
        {'tool': search_tool, 'args': {'query': 'Python tutorials', 'limit': 3}}
    ]

    # Execute all tools concurrently
    print("Executing 5 tools concurrently...")
    start_time = asyncio.get_event_loop().time()

    results = await execute_tools_concurrently(tools_to_run)

    end_time = asyncio.get_event_loop().time()
    print(f"\nCompleted in {end_time - start_time:.2f} seconds\n")

    # Display results
    for i, result in enumerate(results):
        print(f"Result {i+1}: {result}")

    print("\n" + "="*50 + "\n")


# Example 4: Using AsyncAgentRunner
async def agent_runner_demo():
    """Demonstrate AsyncAgentRunner with async tools."""
    print("=== AsyncAgentRunner Demo ===\n")

    # Create tools
    weather_tool = AsyncStandardTool(
        func=fetch_weather,
        name="weather",
        description="Get current weather for a location"
    )

    search_tool = AsyncStructuredTool(
        func=web_search,
        args_schema=SearchArgs,
        name="search",
        description="Search the web for information"
    )

    # Create chat model (requires OpenAI API key)
    # Uncomment and add your API key to run this example
    # chat_model = GPT(model="gpt-4", api_key="your-api-key-here")

    # For demo purposes, we'll skip the actual agent run
    print("To run AsyncAgentRunner:")
    print("1. Set your OpenAI API key")
    print("2. Uncomment the agent creation code below\n")

    # async_agent = AsyncAgentRunner(
    #     model=chat_model,
    #     Agent=AgentType.ReAct,
    #     tools=[weather_tool, search_tool],
    #     max_iterations=4
    # )
    #
    # response = await async_agent.Initialize(
    #     "What's the weather in NYC and find recent AI news?"
    # )
    #
    # print(f"Agent Response: {response.content}")
    # print(f"Tool Calls Made: {len(response.tool_calls)}")

    print("\n" + "="*50 + "\n")


# Example 5: Mixing sync and async tools
async def mixed_tools_demo():
    """Demonstrate using both sync and async tools together."""
    print("=== Mixed Sync/Async Tools Demo ===\n")

    # Async tool
    async def async_calculation(x: int, y: int) -> int:
        """Async calculation (simulating async operation)."""
        await asyncio.sleep(0.1)
        return x * y

    # Note: In AsyncAgentRunner, sync tools still work
    # They're just not executed concurrently
    print("AsyncAgentRunner supports both sync and async tools.")
    print("Async tools are executed with 'await', sync tools run normally.\n")

    print("="*50 + "\n")


async def main():
    """Run all examples."""
    print("\n" + "="*50)
    print("AgentX Async Tools Examples")
    print("="*50 + "\n")

    # Run demos
    await concurrent_execution_demo()
    await agent_runner_demo()
    await mixed_tools_demo()

    print("All examples completed!\n")


if __name__ == "__main__":
    asyncio.run(main())
