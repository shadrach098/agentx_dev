"""
Tour of the new features shipped after 3.1:

  1. Streaming events from Supervisor + HandoffCoordinator
  2. Vector store adapters (Chroma / Qdrant / pgvector -- import shape only)
  3. Prompt optimization via ``Compiled``
  4. Anthropic Batch API (shape-only; needs a real key + willing wait)
  5. Trace viewer wiring (produces a JSONL to drop on viewer/)
  6. New unit tests (mention only; run with ``python -m pytest tests/``)

Sections 1, 3, and 5 run against your OpenAI key (or Anthropic).
Section 2 and 4 print shape info only -- running them requires an
actual DB / batch queue.

Usage:
    python examples/v3_1_1_features_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentx_dev import (
    AgentRunner, AgentType, Claude, GPT, StandardTool, StructuredTool,
    Supervisor, HandoffCoordinator, handoff_tool,
    HashEmbeddings, VectorStore, vector_search_tool,
    EvalCase, contains, called_tool,
    Compiled,
    observability, FileHook, config,
)
from agentx_dev.Agents.Agent import AgentCompletion, ToolCall


HAS_ANTHROPIC = bool(os.getenv("ANTHROPIC_API_KEY"))
HAS_OPENAI = bool(os.getenv("OPENAI_API_KEY"))


def _llm():
    if HAS_ANTHROPIC:
        return Claude(model="claude-sonnet-4-6", enable_prompt_cache=True), "Claude"
    if HAS_OPENAI:
        return GPT(model="gpt-4o"), "GPT"
    raise SystemExit("Set ANTHROPIC_API_KEY or OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# 1. Streaming Supervisor + Handoffs
# ---------------------------------------------------------------------------


def demo_streaming_supervisor():
    print("\n=== 1a. Supervisor.stream() ===")
    if not (HAS_ANTHROPIC or HAS_OPENAI):
        print("(skipped: no API key)"); return
    llm, provider = _llm()

    file_agent = AgentRunner(
        model=llm, agent=AgentType.ReAct, tools=[], verbose=False,
        system_addendum="Answer briefly about file operations.",
    )
    math_agent = AgentRunner(
        model=llm, agent=AgentType.ReAct, tools=[], verbose=False,
        system_addendum="Answer briefly about math.",
    )

    supervisor = Supervisor(
        model=llm,
        agents={
            "file_agent": ("Handles file-system questions.", file_agent),
            "math_agent": ("Handles math and computation.", math_agent),
        },
        max_subtasks=3,
        verbose=False,
    )

    print(f"[{provider}] streaming supervisor events:")
    for event in supervisor.stream("What is 2+2 and how do I list files in Python?"):
        t = event["type"]
        if t == "plan_start":
            print("  * planning...")
        elif t == "plan":
            print(f"  * plan: {len(event['plan'])} steps")
        elif t == "dispatch":
            print(f"  -> dispatch to {event['agent']}: {event['query'][:60]!r}")
        elif t == "subtask_result":
            r = event["result"]
            print(f"  <- {r.agent}: {r.content[:80]!r}")
        elif t == "synthesize_start":
            print("  * synthesizing...")
        elif t == "final":
            print(f"  ANSWER: {event['content'][:150]}")


def demo_streaming_handoffs():
    print("\n=== 1b. HandoffCoordinator.stream() ===")

    class MockRunner:
        def __init__(self, name, next_step=None, final="done"):
            self.name = name; self.next_step = next_step; self.final = final
        def invoke(self, query, chat_history=None):
            tcs = ([ToolCall(name=f"handoff_to_{self.next_step}", args={},
                             result=f"HANDOFF -> {self.next_step}: {query}")]
                   if self.next_step else [])
            return AgentCompletion.from_agent(
                model_name="mock", query=query,
                content=f"[{self.name}] {self.final if not self.next_step else 'routing'}",
                tool_calls=tcs, steps=[], history=[],
            )

    coord = HandoffCoordinator(
        {
            "triage":     MockRunner("triage",     next_step="researcher"),
            "researcher": MockRunner("researcher", next_step="writer"),
            "writer":     MockRunner("writer",     final="Final draft."),
        },
        entry="triage",
    )
    for event in coord.stream("Draft an intro to MVCC."):
        t = event["type"]
        if t == "invoke":
            print(f"  -> invoke {event['agent']} (hop {event['hop']})")
        elif t == "completion":
            print(f"  <- {event['agent']}: {event['content']}")
        elif t == "handoff":
            print(f"  ~~> handoff {event['from']} -> {event['to']}")
        elif t == "final":
            print(f"  ANSWER: {event['content']}")


# ---------------------------------------------------------------------------
# 2. Vector store adapters -- shape only
# ---------------------------------------------------------------------------


def demo_vector_store_adapters():
    print("\n=== 2. Vector store adapters (import shape) ===")
    from agentx_dev.VectorStores import (
        ChromaVectorStore, QdrantVectorStore, PgVectorStore,
    )
    for cls in [ChromaVectorStore, QdrantVectorStore, PgVectorStore]:
        methods = [m for m in ("add", "search", "delete", "clear") if hasattr(cls, m)]
        print(f"  {cls.__name__}  -- methods: {methods}")
    print(
        "  (constructor requires the underlying SDK: chromadb / qdrant-client "
        "/ psycopg -- see docs/advanced/vector-store-adapters.md)"
    )


# ---------------------------------------------------------------------------
# 3. Prompt optimization via Compiled
# ---------------------------------------------------------------------------


def demo_compiled():
    print("\n=== 3. Compiled -- prompt optimization ===")
    if not (HAS_ANTHROPIC or HAS_OPENAI):
        print("(skipped: no API key)"); return

    llm, provider = _llm()

    def build(system_addendum=None):
        return AgentRunner(
            model=llm, agent=AgentType.ReAct, tools=[],
            system_addendum=system_addendum, verbose=False, max_iterations=2,
        )

    trainset = [
        EvalCase("paris",  "Capital of France?",  [contains("Paris")]),
        EvalCase("tokyo",  "Capital of Japan?",   [contains("Tokyo")]),
        EvalCase("berlin", "Capital of Germany?", [contains("Berlin")]),
    ]

    result = Compiled(
        runner_factory=build,
        trainset=trainset,
        teacher_model=llm,
        iterations=2,
        candidates_per_iter=2,
        verbose=True,
    ).compile()

    print(f"  baseline: {result.baseline_score:.1%}")
    print(f"  best:     {result.best_score:.1%}")
    print(f"  addendum: {result.best_addendum[:120]!r}")


# ---------------------------------------------------------------------------
# 4. Batch API -- shape only, doesn't submit
# ---------------------------------------------------------------------------


def demo_batch_shape():
    print("\n=== 4. Claude.batch() (shape only) ===")
    import inspect
    sig = inspect.signature(Claude.batch)
    print(f"  Claude.batch signature: {sig}")
    print(
        "  Accepts a list of strings / message-lists / full request dicts.\n"
        "  Returns results in submission order.\n"
        "  50% cheaper than sync calls -- see docs/advanced/batch-api.md."
    )


# ---------------------------------------------------------------------------
# 5. Trace viewer wiring
# ---------------------------------------------------------------------------


def demo_trace_wiring():
    print("\n=== 5. Trace viewer wiring ===")
    if not (HAS_ANTHROPIC or HAS_OPENAI):
        print("(skipped: no API key)"); return

    trace_path = "./trace_demo.jsonl"
    config.observability_enabled = True
    observability.add_hook(FileHook(trace_path))

    llm, _ = _llm()
    runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[], verbose=False)
    runner.invoke("Say hi in one word.")
    print(f"  wrote {trace_path} -- drop it on viewer/index.html to see the timeline")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    demo_streaming_supervisor()
    demo_streaming_handoffs()
    demo_vector_store_adapters()
    demo_compiled()
    demo_batch_shape()
    demo_trace_wiring()

    print("\n" + "=" * 60)
    print("Everything exercised. See:")
    print("  docs/advanced/vector-store-adapters.md")
    print("  docs/advanced/prompt-optimization.md")
    print("  docs/advanced/batch-api.md")
    print("  docs/advanced/trace-viewer.md")
    print("=" * 60)
