"""
Supervisor / multi-agent orchestration example.

The supervisor decomposes a complex task, dispatches to specialist sub-agents,
and synthesizes their results into a final answer.

Sync version: sub-tasks run sequentially.
Async version: sub-tasks run concurrently via asyncio.gather.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/supervisor_example.py
"""

import asyncio
from pydantic import BaseModel
from agentx_dev import (
    AgentRunner, AsyncAgentRunner, AgentType,
    Supervisor, AsyncSupervisor,
    Claude,
)
from agentx_dev.Tools import StandardTool, StructuredTool
from agentx_dev.AsyncTools import AsyncStandardTool


# --- Tools for specialist agents ---

class CalcArgs(BaseModel):
    expression: str

def calculate(expression: str) -> str:
    """Evaluate a basic math expression."""
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"calc error: {e}"

def fake_search(query: str) -> str:
    """Pretend web search."""
    return f"Search results for '{query}': lorem ipsum facts about {query}."


calc_tool   = StructuredTool(func=calculate, args_schema=CalcArgs, name="calculator", description="Evaluate math expressions.")
search_tool = StandardTool(func=fake_search, name="search", description="Search the web for facts.")


# --- Sync supervisor ---

def run_sync():
    model = Claude(model="claude-sonnet-4-6")

    search_agent = AgentRunner(model=model, Agent=AgentType.ReAct, tools=[search_tool])
    math_agent   = AgentRunner(model=model, Agent=AgentType.ReAct, tools=[calc_tool])

    sup = Supervisor(
        model=model,
        agents={
            "search": ("Web research and fact-finding.", search_agent),
            "math":   ("Calculations and arithmetic.",   math_agent),
        },
        max_subtasks=4,
    )

    result = sup.run("Find facts about Mars, then compute 42 * 1337")
    print("Final answer:", result.content)
    print("\nPlan:", result.plan)
    for sr in result.subtasks:
        print(f"  - [{sr.agent}] {sr.query} -> {sr.content[:80]}")


# --- Async supervisor (sub-agents run concurrently) ---

async def search_async(q: str) -> str:
    """Async search."""
    await asyncio.sleep(0.3)
    return f"async results for {q}"


async def run_async():
    model = Claude(model="claude-sonnet-4-6")

    search_tool_a = AsyncStandardTool(func=search_async, name="search", description="Async web search.")
    research_agent = AsyncAgentRunner(model=model, Agent=AgentType.ReAct, tools=[search_tool_a])

    sup = AsyncSupervisor(
        model=model,
        agents={"research": ("Web research.", research_agent)},
    )

    result = await sup.run("Research the history of Python")
    print("Async final answer:", result.content)


if __name__ == "__main__":
    run_sync()
    print("\n--- Async ---\n")
    asyncio.run(run_async())
