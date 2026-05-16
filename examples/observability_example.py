"""
Example: Observability and Logging Hooks with AgentX

This example demonstrates how to monitor and track agent execution
using the observability system.
"""

from agentx_dev import (
    observability, ConsoleHook, FileHook, MetricsHook, CallbackHook,
    Event, EventType, track_tool_call, track_async_tool_call
)
import time
import asyncio


# Example 1: Basic observability with console hook
def basic_observability_demo():
    """Demonstrate basic observability setup."""
    print("=== Basic Observability Demo ===\n")

    # Add console hook
    console_hook = ConsoleHook(verbose=True, color=True)
    observability.add_hook(console_hook)

    # Emit some events
    event = observability.start_event(
        EventType.AGENT_START,
        data={"query": "What's 2+2?"}
    )

    time.sleep(0.1)  # Simulate work

    observability.end_event(event, data={"result": "4"})

    # Remove hook for next demo
    observability.remove_hook(console_hook)

    print("\n" + "="*50 + "\n")


# Example 2: Metrics collection
def metrics_demo():
    """Demonstrate collecting performance metrics."""
    print("=== Metrics Collection Demo ===\n")

    metrics_hook = MetricsHook()
    observability.add_hook(metrics_hook)

    # Simulate multiple operations
    for i in range(5):
        event = observability.start_event(
            EventType.TOOL_CALL_START,
            data={"tool": f"tool_{i}"}
        )

        # Simulate varying execution times
        time.sleep(0.05 * (i + 1))

        observability.end_event(event, data={"result": f"result_{i}"})

    # Simulate LLM calls
    for i in range(3):
        event = observability.start_event(
            EventType.LLM_CALL_START,
            data={"model": "gpt-4"}
        )

        time.sleep(0.1)

        observability.end_event(event, data={"tokens": 100 + i*10})

    # Get metrics summary
    summary = metrics_hook.get_summary()

    print("Performance Metrics:")
    print(f"\nEvent Counts:")
    for event_type, count in summary['event_counts'].items():
        print(f"  {event_type}: {count}")

    print(f"\nDuration Statistics:")
    for event_type, stats in summary['duration_stats'].items():
        print(f"  {event_type}:")
        print(f"    Count: {stats['count']}")
        print(f"    Total: {stats['total_ms']:.2f}ms")
        print(f"    Average: {stats['avg_ms']:.2f}ms")
        print(f"    Min: {stats['min_ms']:.2f}ms")
        print(f"    Max: {stats['max_ms']:.2f}ms")

    # Reset for next demo
    metrics_hook.reset()
    observability.remove_hook(metrics_hook)

    print("\n" + "="*50 + "\n")


# Example 3: File logging
def file_logging_demo():
    """Demonstrate logging events to file."""
    print("=== File Logging Demo ===\n")

    # Create file hook
    file_hook = FileHook(filepath="agent_events.jsonl", append=False)
    observability.add_hook(file_hook)

    # Generate some events
    print("Logging events to agent_events.jsonl...")

    for i in range(3):
        event = observability.start_event(
            EventType.ITERATION_START,
            data={"iteration": i+1}
        )

        time.sleep(0.05)

        observability.end_event(event, data={"status": "completed"})

    print("Events logged to file: agent_events.jsonl")
    print("Each line is a JSON object representing an event.\n")

    observability.remove_hook(file_hook)

    print("="*50 + "\n")


# Example 4: Custom callback hook
def custom_callback_demo():
    """Demonstrate custom event callbacks."""
    print("=== Custom Callback Demo ===\n")

    # Track tool execution times
    tool_times = {}

    def custom_callback(event: Event):
        """Custom callback to track tool performance."""
        if event.type == EventType.TOOL_CALL_COMPLETE:
            tool_name = event.data.get('tool_name', 'unknown')
            duration = event.duration_ms

            if tool_name not in tool_times:
                tool_times[tool_name] = []
            tool_times[tool_name].append(duration)

    callback_hook = CallbackHook(callback=custom_callback)
    observability.add_hook(callback_hook)

    # Simulate tool calls
    for tool_name in ['weather', 'search', 'calculator']:
        for i in range(3):
            event = observability.start_event(
                EventType.TOOL_CALL_START,
                data={"tool_name": tool_name}
            )

            time.sleep(0.02 * (ord(tool_name[0]) % 5))

            observability.end_event(event)

    # Display custom tracking results
    print("Tool Performance Summary:")
    for tool, times in tool_times.items():
        avg_time = sum(times) / len(times)
        print(f"  {tool}: {avg_time:.2f}ms avg ({len(times)} calls)")

    observability.remove_hook(callback_hook)

    print("\n" + "="*50 + "\n")


# Example 5: Tool tracking decorators
def decorator_demo():
    """Demonstrate automatic tool tracking with decorators."""
    print("=== Tool Tracking Decorators Demo ===\n")

    metrics_hook = MetricsHook()
    observability.add_hook(metrics_hook)

    # Sync tool with tracking
    @track_tool_call
    def calculate(x: int, y: int) -> int:
        """Calculate x + y."""
        time.sleep(0.1)  # Simulate work
        return x + y

    # Async tool with tracking
    @track_async_tool_call
    async def async_fetch(url: str) -> str:
        """Fetch data from URL."""
        await asyncio.sleep(0.1)
        return f"Data from {url}"

    print("Calling tracked sync tool...")
    result1 = calculate(5, 3)
    print(f"  Result: {result1}\n")

    print("Calling tracked async tool...")
    result2 = asyncio.run(async_fetch("https://api.example.com"))
    print(f"  Result: {result2}\n")

    # Show metrics
    summary = metrics_hook.get_summary()
    print(f"Tool calls tracked: {summary['event_counts'].get('tool.call.complete', 0)}")

    observability.remove_hook(metrics_hook)

    print("\n" + "="*50 + "\n")


# Example 6: Error tracking
def error_tracking_demo():
    """Demonstrate error event tracking."""
    print("=== Error Tracking Demo ===\n")

    console_hook = ConsoleHook(verbose=True, color=True)
    observability.add_hook(console_hook)

    # Simulate an error
    try:
        event = observability.start_event(
            EventType.TOOL_CALL_START,
            data={"tool": "risky_operation"}
        )

        # Something goes wrong
        raise ValueError("Simulated error in tool execution")

    except Exception as e:
        # Log the error
        observability.error_event(
            EventType.TOOL_CALL_ERROR,
            error=e,
            data={"tool": "risky_operation"}
        )

    print("\nError events are automatically tracked with details.\n")

    observability.remove_hook(console_hook)

    print("="*50 + "\n")


# Example 7: Complete monitoring setup
def complete_monitoring_demo():
    """Demonstrate a complete monitoring setup."""
    print("=== Complete Monitoring Setup Demo ===\n")

    # Setup multiple hooks
    console_hook = ConsoleHook(verbose=False, color=True)
    metrics_hook = MetricsHook()
    file_hook = FileHook(filepath="complete_monitoring.jsonl", append=False)

    observability.add_hook(console_hook)
    observability.add_hook(metrics_hook)
    observability.add_hook(file_hook)

    # Simulate agent execution
    agent_event = observability.start_event(
        EventType.AGENT_START,
        data={"query": "Complex query requiring multiple steps"}
    )

    # Iteration 1
    iter1 = observability.start_event(EventType.ITERATION_START, data={"iteration": 1})

    llm1 = observability.start_event(EventType.LLM_CALL_START, data={"model": "gpt-4"})
    time.sleep(0.05)
    observability.end_event(llm1, data={"tokens": 150})

    tool1 = observability.start_event(EventType.TOOL_CALL_START, data={"tool": "search"})
    time.sleep(0.03)
    observability.end_event(tool1, data={"result": "search results"})

    observability.end_event(iter1)

    # Iteration 2
    iter2 = observability.start_event(EventType.ITERATION_START, data={"iteration": 2})

    llm2 = observability.start_event(EventType.LLM_CALL_START, data={"model": "gpt-4"})
    time.sleep(0.04)
    observability.end_event(llm2, data={"tokens": 120})

    observability.end_event(iter2)

    observability.end_event(agent_event, data={"final_answer": "Complete response"})

    # Display results
    print("\nMonitoring Results:")
    print("- Console output shows key events")
    print("- Metrics collected for performance analysis")
    print("- Full event log saved to file")

    summary = metrics_hook.get_summary()
    print(f"\nTotal events: {sum(summary['event_counts'].values())}")
    print(f"Agent execution time: {summary['duration_stats'].get('agent.complete', {}).get('total_ms', 0):.2f}ms")

    # Cleanup
    observability.remove_hook(console_hook)
    observability.remove_hook(metrics_hook)
    observability.remove_hook(file_hook)

    print("\n" + "="*50 + "\n")


def main():
    """Run all examples."""
    print("\n" + "="*50)
    print("AgentX Observability Examples")
    print("="*50 + "\n")

    basic_observability_demo()
    metrics_demo()
    file_logging_demo()
    custom_callback_demo()
    decorator_demo()
    error_tracking_demo()
    complete_monitoring_demo()

    print("All examples completed!\n")


if __name__ == "__main__":
    main()
