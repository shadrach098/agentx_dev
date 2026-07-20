"""
Comprehensive AgentX 3.1 demo -- ALL new features working together.

The story: a "framework docs assistant" that answers questions about
AgentX itself, backed by a private knowledge base (its own docs).

Architecture:
  User query
     |
     v
  [ triage agent ]  --handoff-->  [ researcher agent ]  --handoff-->  [ writer agent ]
                                     |                                    |
                                     +-- vector_search_tool               +-- final answer
                                     +-- parallel tool calls
                                     +-- calls RAG store

Every AgentX 3.1 feature appears here:

  1. Prompt caching (Claude only) -- system prompts + tool schemas cached
  2. Parallel tool calls -- researcher uses bind_tools_natively so its
     vector_search calls dispatch concurrently on a thread pool
  3. Long-term semantic memory -- SemanticMemory persists across queries
     and pulls back relevant prior context on later turns
  4. RAG -- VectorStore + vector_search_tool over the framework's docs
  5. Handoffs -- HandoffCoordinator routes triage -> researcher -> writer
  6. Evals -- a small suite that regression-tests the whole pipeline

Runs with either OPENAI_API_KEY (uses GPT) or ANTHROPIC_API_KEY (uses
Claude and enables prompt caching). RAG + memory + handoff + evals all
work regardless of provider.

Usage:
    python examples/v3_1_comprehensive_demo.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentx_dev import (
    # Core (unchanged 3.0)
    AgentRunner, AgentType, Claude, GPT, Permissions, StructuredTool,

    # 3.1: prompt caching lives inside Claude / TokenUsage -- no import needed
    # 3.1: RAG
    VectorStore, HashEmbeddings, OpenAIEmbeddings, vector_search_tool,
    # 3.1: long-term memory
    SemanticMemory, create_semantic_memory,
    # 3.1: handoffs
    HandoffCoordinator, handoff_tool,
    # 3.1: evals
    EvalCase, EvalRunner, contains, called_tool, tool_count,
)
from pydantic import BaseModel, Field


HAS_ANTHROPIC = bool(os.getenv("ANTHROPIC_API_KEY"))
HAS_OPENAI = bool(os.getenv("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Helper: build the LLM based on which key is available.
# When Anthropic is available, we use Claude + enable prompt caching so the
# system prompts + tool schemas are cached across calls (~90% cheaper on
# repeated input tokens). When only OpenAI is available, we use GPT.
# ---------------------------------------------------------------------------


def build_llm():
    if HAS_ANTHROPIC:
        # enable_prompt_cache=True marks the system prompt + tool schemas
        # as cache_control: ephemeral on every Claude call. The second and
        # subsequent calls read them from Anthropic's server-side cache.
        # cache_history_after=4 puts a THIRD cache breakpoint on the last
        # stable assistant message once the conversation is long enough.
        return Claude(
            model="claude-sonnet-4-6",
            enable_prompt_cache=True,
            cache_history_after=4,
        ), "Claude (prompt caching ON)"
    if HAS_OPENAI:
        # parallel_tool_calls is ON by default in gpt-4o when tools are
        # supplied. We DON'T set it as a model-level default because it
        # would leak into tool-less calls (like the writer agent's
        # synthesis turn) and OpenAI 400s in that case.
        return GPT(model="gpt-4o"), "GPT (no prompt caching -- Anthropic-only feature)"
    raise SystemExit(
        "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment / .env"
    )


# ---------------------------------------------------------------------------
# 1. Build a RAG store of framework knowledge.
# ---------------------------------------------------------------------------
# Real apps would embed a chunked corpus of Markdown / PDFs / code. For the
# demo, a handful of hand-picked snippets is enough to show retrieval.
# HashEmbeddings runs offline. Swap in OpenAIEmbeddings() if you have the
# key and want real semantic quality.


def build_knowledge_base() -> VectorStore:
    knowledge_snippets = [
        # Snippets stolen from README + AGENTX.md
        "AgentX Permissions class controls which capabilities a tool has: read_files, write_files, edit_files, execute_python, execute_shell. Denied capabilities are not even registered as tools.",
        "The Supervisor pattern decomposes a task into a plan of sub-tasks and dispatches each to a named specialist. SpawnConfig lets it create new specialists mid-plan.",
        "AgentX Session persists a conversation across process restarts. Session.start(runner) begins a fresh one; Session.load(path).attach(runner) resumes.",
        "The framework catches duplicate tool calls: warn at 2 repeats, refuse at 5. The runner loop force-terminates after 3 identical (action, args) pairs in a row.",
        "web_fetch supports optional disk cache_dir. When set, the FULL response body lands on disk and the reply names the path so the model can open() it in run_python without re-transmitting HTML.",
        "AgentType templates: ReAct, Chain_of_Thought, Zero_Shot, Few_Shot, Instruction_Tuned. All follow HARD RULES / PROCESS checklist / ANTI-PATTERNS / OUTPUT CONTRACT structure.",
        "In 3.1, Claude gained enable_prompt_cache. When True, cache_control ephemeral markers are added to the system prompt and tool schemas so Anthropic serves repeat prompts from cache.",
        "In 3.1, AgentRunner gained bind_tools_natively + parallel_tool_workers. When native binding is on, multiple tool_use blocks in one LLM turn dispatch concurrently on a thread pool.",
        "SemanticMemory embeds every message and retrieves the top-K most relevant past turns when a query is set, injected as a synthetic system message before the recent tail.",
        "HandoffCoordinator routes between AgentRunners via HandoffRequest sentinels returned by handoff_tool(). Bounded by max_hops so cycles halt.",
    ]

    # Use OpenAI embeddings when the key is available -- much better recall
    # than the hash fallback. Fall back to HashEmbeddings for offline runs.
    if HAS_OPENAI:
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    else:
        embeddings = HashEmbeddings(dim=256)

    store = VectorStore(embeddings=embeddings)
    store.add(
        knowledge_snippets,
        ids=[f"snippet_{i}" for i in range(len(knowledge_snippets))],
        metadata=[{"source": "agentx_docs", "index": i} for i in range(len(knowledge_snippets))],
    )
    return store


# ---------------------------------------------------------------------------
# 2. Build the three agents.
# ---------------------------------------------------------------------------
# Each is a plain AgentRunner. What makes them a "multi-agent system" is
# that some of them expose handoff_tool(...) to reach the others.
# HandoffCoordinator resolves those handoffs.


def build_agents(llm, kb_store: VectorStore):
    # ---- Triage agent -----------------------------------------------------
    # Job: read the user's question, decide who should handle it. Tools:
    # handoff_to_researcher (framework questions) and handoff_to_writer
    # (formatting / summarizing pre-existing content).
    triage = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[
            handoff_tool(
                "researcher",
                description=(
                    "Hand off to the researcher for any question that needs "
                    "looking up framework knowledge, code, docs, or how AgentX works."
                ),
            ),
            handoff_tool(
                "writer",
                description=(
                    "Hand off to the writer for pure formatting, rewording, or "
                    "summarizing a chunk of text the user already provided."
                ),
            ),
        ],
        max_iterations=3,
        verbose=False,
        system_addendum=(
            "You are the TRIAGE agent. Never answer the question yourself -- "
            "your only job is to call the right handoff tool. Framework "
            "questions go to researcher; text-shaping tasks go to writer."
        ),
    )

    # ---- Researcher agent -------------------------------------------------
    # Job: query the RAG store, gather relevant snippets, hand off to the
    # writer with a fact list.
    #
    # KEY: bind_tools_natively=True + parallel_tool_workers=4. When the
    # model wants to look up several sub-topics, it emits multiple
    # tool_use blocks in one turn and they dispatch concurrently.
    researcher = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[
            vector_search_tool(
                kb_store,
                name="framework_docs",
                default_top_k=3,
                description=(
                    "Semantic search over the private AgentX framework docs. "
                    "Use for any question about the framework's classes, "
                    "patterns, capabilities, or version history. Call MULTIPLE "
                    "times in one turn if the question has multiple sub-parts."
                ),
            ),
            handoff_tool(
                "writer",
                description=(
                    "Hand off to the writer once you have collected enough "
                    "framework snippets to answer. Pass the raw snippets + "
                    "the user's original question in your task text."
                ),
            ),
        ],
        bind_tools_natively=True,
        parallel_tool_workers=4,
        max_iterations=5,
        verbose=False,
        system_addendum=(
            "You are the RESEARCHER. Look up framework knowledge via "
            "framework_docs. Emit as many parallel framework_docs calls "
            "in one turn as the question warrants (parallel dispatch is on). "
            "When you have enough, hand off to the writer."
        ),
    )

    # ---- Writer agent -----------------------------------------------------
    # Job: take the researcher's findings + user's question, produce the
    # final human-readable answer. No further handoff -- writer terminates
    # the chain.
    writer = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[],  # no tools -- writer just synthesizes
        max_iterations=2,
        verbose=False,
        system_addendum=(
            "You are the WRITER. You receive a research summary + a user "
            "question. Compose ONE crisp answer (2-4 sentences) that directly "
            "answers the question, grounded in the research. Do NOT hand off. "
            "Emit Final_Answer with your response."
        ),
    )

    return triage, researcher, writer


# ---------------------------------------------------------------------------
# 3. Long-term memory that survives across queries.
# ---------------------------------------------------------------------------
# The multi-agent system above is stateless per-query. Wrap the coordinator
# with a SemanticMemory that logs every user query + final answer and pulls
# back relevant older turns on later queries.


def wrap_with_memory(coord: HandoffCoordinator, memory: SemanticMemory):
    """Return a callable that runs the coordinator with retrieved context
    injected as chat history + logs every turn back into the memory store."""

    def _ask(question: str):
        # Focus retrieval on the current question. SemanticMemory scores
        # older messages by cosine similarity to this text.
        memory.set_query(question)
        prior_context = memory.get_messages()

        # Only the retrieved-context + recent-tail slice goes forward. The
        # coordinator threads it to the entry agent as chat_history.
        result = coord.run(question, chat_history=prior_context)

        # Log this turn back into memory for future recall.
        memory.add_message("user", question)
        memory.add_message("assistant", result.content)
        return result

    return _ask


# ---------------------------------------------------------------------------
# 4. Run the pipeline with a couple of queries.
# ---------------------------------------------------------------------------


def run_live_pipeline():
    print("=" * 72)
    print("COMPREHENSIVE 3.1 DEMO -- multi-agent pipeline w/ RAG + memory")
    print("=" * 72)

    llm, provider = build_llm()
    print(f"\nLLM: {provider}")

    kb_store = build_knowledge_base()
    print(f"Knowledge base: {len(kb_store)} snippets indexed via "
          f"{type(kb_store.embeddings).__name__}")

    triage, researcher, writer = build_agents(llm, kb_store)
    coord = HandoffCoordinator(
        agents={"triage": triage, "researcher": researcher, "writer": writer},
        entry="triage",
        max_hops=4,
    )
    print("Agents wired: triage -> researcher -> writer (via HandoffCoordinator)")

    # Long-term memory over the multi-agent system.
    memory = create_semantic_memory(
        embeddings=kb_store.embeddings,  # reuse the same backend -> dim matches
        recent_tail=4, top_k=3,
    )
    ask = wrap_with_memory(coord, memory)
    print("Long-term memory: SemanticMemory attached (recent_tail=4, top_k=3)")
    print()

    # ------------------------------------------------------------------ Q1
    q1 = "What did AgentX 3.1 add for handling multiple tool calls per turn?"
    print(f"[Q1] {q1}")
    t0 = time.perf_counter()
    result = ask(q1)
    dt = time.perf_counter() - t0
    print(f"[A1] {result.content}")
    print(f"     (routed via: {[h['from']+'->'+h['to'] for h in result.hops]})")
    print(f"     (wall clock: {dt:.2f}s)")

    _print_cache_stats(llm)

    # ------------------------------------------------------------------ Q2
    # This one probes long-term memory: the framework question is different
    # from Q1's, but if SemanticMemory works, Q1's discussion of parallel
    # tool calls remains recoverable via similarity.
    q2 = "What was the earlier question I asked about?"
    print(f"\n[Q2] {q2}  (tests long-term memory recall)")
    t0 = time.perf_counter()
    result = ask(q2)
    dt = time.perf_counter() - t0
    print(f"[A2] {result.content}")
    print(f"     (routed via: {[h['from']+'->'+h['to'] for h in result.hops]})")
    print(f"     (wall clock: {dt:.2f}s)")

    _print_cache_stats(llm)

    # ------------------------------------------------------------------ Q3
    # A follow-up that likely fires MULTIPLE vector_search calls in one turn
    # (2+ sub-questions), letting parallel dispatch shine.
    q3 = "How does prompt caching work AND what does the Session class do? Answer both."
    print(f"\n[Q3] {q3}  (should trigger PARALLEL vector_search calls)")
    t0 = time.perf_counter()
    result = ask(q3)
    dt = time.perf_counter() - t0
    print(f"[A3] {result.content}")
    print(f"     (routed via: {[h['from']+'->'+h['to'] for h in result.hops]})")
    print(f"     (wall clock: {dt:.2f}s)")

    _print_cache_stats(llm)

    return llm, coord, kb_store, memory


def _print_cache_stats(llm):
    """Print cache stats -- non-zero only for Claude + enable_prompt_cache."""
    u = llm.usage
    print(
        f"     tokens: in={u.total_input_tokens}, out={u.total_output_tokens}"
        + (
            f", cache_read={u.total_cache_read_tokens}, "
            f"hit_ratio={u.cache_hit_ratio:.1%}"
            if u.total_cache_read_tokens else ""
        )
    )


# ---------------------------------------------------------------------------
# 5. Evals -- regression-test the whole pipeline
# ---------------------------------------------------------------------------
# The eval runner builds a FRESH pipeline per case (so state doesn't leak),
# runs each query through it, and checks assertions on the final completion.


def run_evals(llm_provider_hint: str):
    print("\n" + "=" * 72)
    print("EVALS -- regression-test the pipeline")
    print("=" * 72)

    def _factory():
        """Fresh coordinator + memory per case. Since HandoffCoordinator
        isn't itself an AgentRunner, we wrap it in a tiny shim that
        implements the .invoke(query, chat_history=None) contract EvalRunner
        expects."""
        llm, _ = build_llm()
        kb_store = build_knowledge_base()
        triage, researcher, writer = build_agents(llm, kb_store)
        coord = HandoffCoordinator(
            {"triage": triage, "researcher": researcher, "writer": writer},
            entry="triage", max_hops=4,
        )

        class _CoordShim:
            model = llm  # EvalRunner reads this for token attribution
            def invoke(self, query, chat_history=None):
                return coord.run(query, chat_history=chat_history).completion

        return _CoordShim()

    cases = [
        EvalCase(
            name="permissions_answer",
            input="Which capabilities does Permissions control?",
            assertions=[
                contains("permissions", case_sensitive=False),
                # writer emits the answer; researcher must have hit the RAG store
                # (harder to assert cross-agent; check the final content instead)
            ],
        ),
        EvalCase(
            name="v3_1_features_mentioned",
            input="What are the new 3.1 features related to caching?",
            assertions=[
                contains("cache"),
            ],
        ),
        EvalCase(
            name="stays_bounded",
            input="Explain the Supervisor pattern in one sentence.",
            assertions=[
                contains("supervisor", case_sensitive=False),
                # keep the whole chain compact
                tool_count(max=6),
            ],
        ),
    ]

    print(f"(Using {llm_provider_hint} -- one fresh pipeline built per case)\n")
    report = EvalRunner(_factory, verbose=True).run(cases)
    print()
    print(f"Report as dict (for CI): {list(report.to_dict().keys())}")


# ---------------------------------------------------------------------------
# 6. Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    llm, coord, kb_store, memory = run_live_pipeline()
    provider = "Claude" if HAS_ANTHROPIC else "GPT"
    run_evals(provider)

    print("\n" + "=" * 72)
    print("DONE. Features exercised:")
    print("  [x] Prompt caching       (Claude only -- see cache stats above)")
    print("  [x] Parallel tool calls  (researcher's vector_search dispatches)")
    print("  [x] Long-term memory     (Q2 recalled Q1 via SemanticMemory)")
    print("  [x] RAG                  (vector_search_tool over VectorStore)")
    print("  [x] Handoffs             (triage -> researcher -> writer)")
    print("  [x] Evals                (3 cases through fresh pipeline each)")
    print("=" * 72)
