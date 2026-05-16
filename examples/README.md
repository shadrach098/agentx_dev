# AgentX Examples

This directory contains practical examples demonstrating the enhanced features of AgentX framework.

## Examples Overview

### 1. `async_example.py` - Async Tool Execution
**What it demonstrates:**
- Creating async tools with `AsyncStandardTool` and `AsyncStructuredTool`
- Concurrent execution of multiple tools
- Using `AsyncAgentRunner`
- Mixing sync and async tools

**Run it:**
```bash
python async_example.py
```

**Key takeaways:**
- Async tools enable concurrent I/O operations
- Significant performance improvements for API calls
- Backward compatible with sync tools

---

### 2. `caching_example.py` - Tool Result Caching
**What it demonstrates:**
- `InMemoryCache` for fast caching with TTL
- `LRUCache` for capacity-limited caching
- `FileCache` for persistent caching
- `@cached_tool` decorator for automatic caching
- Cache management operations

**Run it:**
```bash
python caching_example.py
```

**Key takeaways:**
- Caching avoids redundant expensive operations
- Different cache types for different use cases
- TTL support for automatic expiration
- Decorator makes caching transparent

---

### 3. `observability_example.py` - Monitoring & Metrics
**What it demonstrates:**
- Setting up observability hooks
- `ConsoleHook` for colored console output
- `MetricsHook` for performance tracking
- `FileHook` for JSON logging
- `CallbackHook` for custom processing
- `@track_tool_call` decorator

**Run it:**
```bash
python observability_example.py
```

**Key takeaways:**
- Monitor every step of agent execution
- Collect performance metrics automatically
- Custom callbacks for integration
- Zero-config observability

---

### 4. `complete_example.py` - All Features Together
**What it demonstrates:**
- Complete multi-turn conversation setup
- All enhanced features working together
- Real-world usage patterns
- Best practices

**Run it:**
```bash
python complete_example.py
```

**Key takeaways:**
- How features integrate seamlessly
- Production-ready setup
- Memory + Caching + Observability + Async

---

## Quick Start

### Basic Example (No API Key Needed)

```python
# Run any example without OpenAI API
python async_example.py
python caching_example.py
python observability_example.py
```

These examples demonstrate the features without requiring an OpenAI API key.

### With OpenAI API

To use the actual agent functionality:

1. Set your API key:
```bash
export OPENAI_API_KEY='your-key-here'
```

2. Uncomment the agent creation code in examples

3. Run the example

---

## Learning Path

**Recommended order for learning:**

1. **Start with caching** (`caching_example.py`)
   - Easiest to understand
   - Immediate performance benefits
   - No async complexity

2. **Add observability** (`observability_example.py`)
   - See what's happening inside agents
   - Learn event tracking
   - Understand performance metrics

3. **Try async tools** (`async_example.py`)
   - Understand async benefits
   - See concurrent execution
   - Compare performance

4. **Complete example** (`complete_example.py`)
   - See everything together
   - Real-world patterns
   - Production setup

---

## Example Output

### Caching Performance
```
First call (cache miss):
  [EXECUTING] expensive_operation('test query')
  Result: Result for: test query
  Time: 2.01s

Second call (cache hit):
  Result: Result for: test query
  Time: 0.00s
```

### Concurrent Execution
```
Executing 5 tools concurrently...
Fetching weather for New York...
Fetching weather for Los Angeles...
Searching for 'AI news'...

Completed in 1.05 seconds
(vs 3+ seconds sequentially)
```

### Observability Output
```
[tool.call.start]
[tool.call.complete] (123.45ms)
[llm.call.start]
[llm.call.complete] (456.78ms)

Performance Metrics:
  tool.call.complete: 5 calls, avg 120ms
  llm.call.complete: 3 calls, avg 450ms
```

---

## Common Patterns

### Pattern 1: Cached Async Tool
```python
from agentx_dev import AsyncStandardTool, InMemoryCache, cached_tool

cache = InMemoryCache(default_ttl=300)

@cached_tool(cache, ttl=60)
async def api_call(query: str) -> str:
    return await fetch_from_api(query)

tool = AsyncStandardTool(func=api_call, name="api")
```

### Pattern 2: Monitored Agent
```python
from agentx_dev import observability, ConsoleHook, MetricsHook

observability.add_hook(ConsoleHook(verbose=True))
metrics = MetricsHook()
observability.add_hook(metrics)

# Use agent - events auto-tracked
response = agent.Initialize("query")

# Get metrics
print(metrics.get_summary())
```

### Pattern 3: Memory-Managed Conversation
```python
from agentx_dev import TokenLimitedMemory

memory = TokenLimitedMemory(max_tokens=4000)

for user_query in conversation:
    response = agent.Initialize(
        user_query,
        ChatHistory=memory.get_messages()
    )

    memory.add_message("user", user_query)
    memory.add_message("assistant", response.content)
```

---

## Troubleshooting

### Import Errors
```python
# Make sure AgentX is installed
pip install agentx-dev

# Or install in development mode
pip install -e .
```

### OpenAI API Errors
```python
# Set API key
import os
os.environ['OPENAI_API_KEY'] = 'your-key'

# Or pass directly
from agentx_dev import GPT
chat_model = GPT(api_key='your-key')
```

### Async Errors
```python
# Run async functions with asyncio
import asyncio

async def main():
    result = await async_agent.Initialize("query")

asyncio.run(main())
```

---

## Next Steps

After running these examples:

1. Read `ENHANCEMENTS.md` for detailed documentation
2. Check `ENHANCEMENT_SUMMARY.md` for overview
3. Explore the source code in `agentx_dev/`
4. Build your own agent with these features!

---

## Questions?

- **Documentation**: See `ENHANCEMENTS.md`
- **Issues**: https://github.com/shadrach098/Bruce_framework/issues
- **Source**: https://github.com/shadrach098/Bruce_framework
