"""
Sync AgentRunner quickstart — works with GPT or Claude.

Run:
    export OPENAI_API_KEY=sk-...        # for GPT
    export ANTHROPIC_API_KEY=sk-ant-... # for Claude
    python examples/sync_quickstart.py
"""

from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, GPT, Claude
from agentx_dev.Tools import StandardTool, StructuredTool


# --- Define tools ---

class MultiplyArgs(BaseModel):
    a: int
    b: int


def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 22°C, partly cloudy"


multiply_tool = StructuredTool(
    func=multiply,
    args_schema=MultiplyArgs,
    name="multiply",
    description="Multiply two numbers.",
)

weather_tool = StandardTool(
    func=get_weather,
    name="weather",
    description="Get the current weather for a given city name.",
)


# --- Pick a model: GPT or Claude ---

# OpenAI
model = GPT(model="gpt-4o", temperature=0.3)

# Or Anthropic (uncomment to use)
# model = Claude(model="claude-sonnet-4-6", max_tokens=2048)


# --- Build the runner ---

runner = AgentRunner(
    model=model,
    Agent=AgentType.ReAct,
    tools=[multiply_tool, weather_tool],
    max_iterations=6,
)


# --- Run multiple calls on the SAME runner (now safe — was broken before the fix) ---

r1 = runner.Initialize("What is 47 times 13?")
print("Answer 1:", r1.content)
print("Tool calls:", [tc.name for tc in r1.tool_calls])

r2 = runner.Initialize("What's the weather in Toronto?")
print("Answer 2:", r2.content)

# Full conversation history is now returned (was None before the fix)
print(f"\nFinal history length: {len(r2.history)} messages")
