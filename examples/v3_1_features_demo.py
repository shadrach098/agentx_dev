"""
End-to-end tour of the 3.1 power features.

What this demonstrates:
  1. Prompt caching on Claude -- second call reads the system prompt from
     Anthropic's cache, ~90% cheaper on that portion.
  2. Semantic long-term memory + RAG -- an in-memory VectorStore serves
     the agent's queries via a vector_search tool.
  3. Parallel tool calls per turn -- bind_tools_natively=True dispatches
     multiple tool_use blocks concurrently on a thread pool.
  4. Agent-to-agent handoffs -- a triage agent routes to a math specialist
     via HandoffCoordinator.
  5. Evals -- pass/fail regression cases run in a factory.

Nothing here needs external services beyond an ANTHROPIC_API_KEY (only
for the live-model runs; the eval + handoff + RAG parts can run against
mock models offline). Set ANTHROPIC_API_KEY in your env, or comment out
the live sections and only run the offline ones.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/v3_1_features_demo.py
"""

from __future__ import annotations

import os
import sys

# Add repo root to path so this runs from a fresh checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentx_dev import (
    AgentRunner, AgentType, Claude, GPT, Permissions,
    VectorStore, HashEmbeddings, vector_search_tool,
    SemanticMemory, HandoffCoordinator, handoff_tool,
    EvalCase, EvalRunner, contains, called_tool, tool_count,
    StructuredTool,
)
from pydantic import BaseModel, Field


HAS_ANTHROPIC = bool(os.getenv("ANTHROPIC_API_KEY"))
HAS_OPENAI = bool(os.getenv("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# 1. Prompt caching
# ---------------------------------------------------------------------------


def demo_prompt_caching():
    print("\n=== 1. Prompt caching (Anthropic-only) ===")
    if not HAS_ANTHROPIC:
        print("(skipped: prompt caching is an Anthropic feature; "
              "set ANTHROPIC_API_KEY to see it in action)")
        return
    llm = Claude(model="claude-sonnet-4-6", enable_prompt_cache=True)
    runner = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[],
        max_iterations=2,
        verbose=False,
    )
    for i in range(2):
        result = runner.invoke(f"In one word, say hello #{i}.")
        u = llm.usage
        print(
            f"  call {i + 1}: content={result.content[:40]!r} "
            f"input={u.last_input_tokens} cache_read={u.last_cache_read_tokens} "
            f"cache_creation={u.last_cache_creation_tokens}"
        )
    print(f"  cumulative cache_hit_ratio: {llm.usage.cache_hit_ratio:.1%}")


# ---------------------------------------------------------------------------
# 2. Semantic memory + RAG (offline via HashEmbeddings)
# ---------------------------------------------------------------------------


def demo_rag():
    print("\n=== 2. RAG via vector_search_tool ===")
    store = VectorStore(embeddings=HashEmbeddings())
    store.add(
        [
            "Postgres uses MVCC (multi-version concurrency control) so readers "
            "never block writers and vice versa.",
            "SQLite stores an entire database inside a single ordinary file. It "
            "is embeddable and needs no separate server.",
            "Redis is an in-memory data-structure store often used as a cache "
            "or message broker.",
        ],
        metadata=[{"topic": "postgres"}, {"topic": "sqlite"}, {"topic": "redis"}],
    )
    tool = vector_search_tool(store, default_top_k=2)
    # Call the tool directly (no LLM) to show the returned shape.
    print(tool.func(query="how does postgres handle concurrency?", top_k=2))


def demo_semantic_memory():
    print("\n=== 3. SemanticMemory recall ===")
    mem = SemanticMemory(
        embeddings=HashEmbeddings(), recent_tail=2, top_k=2, min_score=0.0,
    )
    mem.add_message("system", "You remember everything the user tells you.")
    mem.add_message("user", "My dog is named Rex and he's a border collie.")
    mem.add_message("assistant", "Noted -- Rex the border collie.")
    mem.add_message("user", "My favorite pizza topping is anchovies.")
    mem.add_message("assistant", "Noted -- anchovies.")
    mem.add_message("user", "I live in Accra.")
    mem.add_message("assistant", "Noted -- Accra.")

    # Recent tail is 2, so the older Rex message would fall off a sliding-
    # window memory. SemanticMemory pulls it back when the query mentions dog.
    mem.set_query("what's my dog's breed?")
    for m in mem.get_messages():
        print(f"  [{m['role']}] {m['content'][:120]}")


# ---------------------------------------------------------------------------
# 4. Parallel tool calls per turn
# ---------------------------------------------------------------------------


def demo_parallel_tools():
    print("\n=== 4. Parallel tool calls (native binding) ===")
    if not (HAS_ANTHROPIC or HAS_OPENAI):
        print("(skipped: set ANTHROPIC_API_KEY or OPENAI_API_KEY to run)")
        return

    class WeatherArgs(BaseModel):
        city: str = Field(..., description="City name.")

    def _weather(city: str) -> str:
        # Sleep to make the parallel win visible on wall-clock.
        import time
        time.sleep(0.5)
        return f"{city}: 22C, sunny"

    # ONE tool the model can call multiple times per turn -- that's the
    # native-parallel-tool-calls contract on both GPT and Claude. Three
    # separate tool objects for three cities would work too, but this
    # shape is closer to real RAG patterns ("call vector_search 3 times").
    tools = [StructuredTool(
        func=_weather, args_schema=WeatherArgs,
        name="get_weather",
        description="Return the current weather for a single city.",
    )]

    if HAS_ANTHROPIC:
        llm = Claude(model="claude-sonnet-4-6")
        provider = "Claude"
    else:
        # gpt-4o has parallel_tool_calls on by default; force it on for clarity.
        llm = GPT(model="gpt-4o", parallel_tool_calls=True)
        provider = "GPT"

    runner = AgentRunner(
        model=llm, agent=AgentType.ReAct, tools=tools,
        bind_tools_natively=True, parallel_tool_workers=4,
        max_iterations=3, verbose=False,
    )
    import time as _t
    t0 = _t.perf_counter()
    result = runner.invoke(
        "Fetch the current weather for London, Paris, and Tokyo, all in "
        "parallel using the get_weather tool. Then briefly summarize."
    )
    dt = _t.perf_counter() - t0
    print(f"  provider: {provider}")
    print(f"  answer: {result.content[:200]}")
    print(f"  tool calls made: {len(result.tool_calls)}")
    print(f"  wall clock: {dt:.2f}s (sequential would be >= 1.5s at 0.5s each)")


# ---------------------------------------------------------------------------
# 5. Agent-to-agent handoffs (offline mock)
# ---------------------------------------------------------------------------


def demo_handoffs():
    print("\n=== 5. Handoffs (offline mock) ===")

    class MockRunner:
        def __init__(self, name, next_step=None, final="done"):
            self.name = name
            self.next_step = next_step
            self.final = final

        def invoke(self, query, chat_history=None):
            from agentx_dev.Agents.Agent import AgentCompletion, ToolCall
            tcs = []
            if self.next_step:
                # Emit the exact string the coordinator scans for.
                tcs = [ToolCall(
                    name=f"handoff_to_{self.next_step}",
                    args={},
                    result=f"HANDOFF -> {self.next_step}: {query}",
                )]
            content = f"[{self.name}] {self.final}" if not self.next_step else f"[{self.name}] handing off..."
            return AgentCompletion.from_agent(
                model_name="mock", query=query, content=content,
                tool_calls=tcs, steps=[], history=[],
            )

    coord = HandoffCoordinator(
        {
            "triage": MockRunner("triage", next_step="researcher"),
            "researcher": MockRunner("researcher", next_step="writer"),
            "writer": MockRunner("writer", final="Final draft ready."),
        },
        entry="triage",
    )
    result = coord.run("Write a summary of MVCC.")
    print(f"  final: {result.content}")
    print(f"  hops:  {result.hops}")


# ---------------------------------------------------------------------------
# 6. Evals
# ---------------------------------------------------------------------------


def demo_evals():
    print("\n=== 6. Evals (offline mock) ===")
    from agentx_dev.Agents.Agent import AgentCompletion, ToolCall

    class MockRunner:
        def __init__(self, model=None):
            self.model = None
        def invoke(self, query, chat_history=None):
            content = "Paris is the capital of France." if "France" in query else "Result: 12467"
            tcs = [ToolCall(name="calculator", args={"Input": "137*91"}, result="12467")] if "137" in query else []
            return AgentCompletion.from_agent(
                model_name="mock", query=query, content=content,
                tool_calls=tcs, steps=["step 1"], history=[],
            )

    cases = [
        EvalCase(
            name="paris_capital", input="What's the capital of France?",
            assertions=[contains("Paris")],
        ),
        EvalCase(
            name="uses_calculator", input="What is 137 * 91?",
            assertions=[called_tool("calculator"), contains("12467"), tool_count(min=1)],
        ),
    ]
    report = EvalRunner(lambda: MockRunner(), verbose=False).run(cases)
    print(report.summary())


if __name__ == "__main__":
    demo_prompt_caching()
    demo_rag()
    demo_semantic_memory()
    demo_parallel_tools()
    demo_handoffs()
    demo_evals()
