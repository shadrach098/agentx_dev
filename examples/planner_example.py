"""
PlanningAgent example — plan-then-execute pattern.

The planner produces a structured step-by-step plan FIRST, then the underlying
runner executes the task with the plan injected as context. This dramatically
improves reliability on multi-step tasks.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/planner_example.py
"""

import asyncio
from pydantic import BaseModel
from agentx_dev import PlanningAgent, AsyncPlanningAgent, AgentType, Claude
from agentx_dev.Tools import StandardTool, StructuredTool
from agentx_dev.AsyncTools import AsyncStandardTool


class CalcArgs(BaseModel):
    expression: str


def calculator(expression: str) -> str:
    """Evaluate a basic math expression."""
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"calc error: {e}"


def lookup(topic: str) -> str:
    """Look up information about a topic (simulated)."""
    facts = {
        "earth": "Earth has a radius of about 6371 km.",
        "mars":  "Mars has a radius of about 3389 km.",
    }
    return facts.get(topic.lower(), f"No data for '{topic}'.")


calc_tool   = StructuredTool(func=calculator, args_schema=CalcArgs, name="calculator", description="Math evaluator.")
lookup_tool = StandardTool(func=lookup, name="lookup", description="Look up basic facts about a topic.")


def run_sync():
    model = Claude(model="claude-sonnet-4-6")

    agent = PlanningAgent(
        model=model,
        Agent=AgentType.ReAct,
        tools=[lookup_tool, calc_tool],
        max_iterations=8,
        max_plan_steps=6,
    )

    result = agent.Initialize(
        "Find the radius of Mars, then compute its surface area assuming a perfect sphere."
    )

    print("Plan:")
    for step in result.plan:
        print(" ", step)

    print("\nFinal answer:", result.content)


async def lookup_async(topic: str) -> str:
    """Async lookup."""
    await asyncio.sleep(0.1)
    return lookup(topic)


async def run_async():
    model = Claude(model="claude-sonnet-4-6")

    async_lookup = AsyncStandardTool(func=lookup_async, name="lookup", description="Async fact lookup.")

    agent = AsyncPlanningAgent(
        model=model,
        Agent=AgentType.ReAct,
        tools=[async_lookup],
        max_plan_steps=4,
    )

    result = await agent.Initialize("Tell me about Earth")
    print("Async plan:", result.plan)
    print("Async answer:", result.content)


if __name__ == "__main__":
    run_sync()
    print("\n--- Async ---\n")
    asyncio.run(run_async())
