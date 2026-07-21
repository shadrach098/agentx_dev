"""
Agentic RAG chatbot -- the runnable version of use case #13.

What this demonstrates end-to-end:
  * Two vector stores (public knowledge base + per-user notes)
  * Parallel per-turn retrieval via bind_tools_natively
  * SemanticMemory for long-tail user context
  * Session persistence across restarts
  * Prompt caching (Claude only)
  * Cost budget per session
  * Citation-enforced system prompt
  * Refusal when the corpus doesn't cover the answer

Prereqs:
  - export ANTHROPIC_API_KEY=... (or OPENAI_API_KEY for the GPT fallback)
  - export OPENAI_API_KEY=... (for embeddings; HashEmbeddings if unset)
  - a folder ./data/kb with markdown files to index

Usage:
    python examples/agentic_rag_demo.py "What's the refund policy?"
    python examples/agentic_rag_demo.py --user alice
    python examples/agentic_rag_demo.py --demo   # runs a scripted 3-turn session
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentx_dev import (
    AgentRunner, AgentType, Claude, GPT,
    HashEmbeddings, OpenAIEmbeddings, VectorStore, vector_search_tool,
    SemanticMemory, Session,
)


# ---------------------------------------------------------------------------
# Storage layout (all under ./data/)
# ---------------------------------------------------------------------------

DATA_DIR = Path("./data")
KB_DIR = DATA_DIR / "kb"
USERS_DIR = DATA_DIR / "users"
SESSIONS_DIR = DATA_DIR / "sessions"
KB_INDEX_PATH = DATA_DIR / "kb.json"


def _embeddings():
    """Prefer OpenAI embeddings when the key is set; fall back to hashes so
    the demo runs offline. Same interface either way."""
    if os.getenv("OPENAI_API_KEY"):
        return OpenAIEmbeddings(model="text-embedding-3-small")
    print("[embeddings] using HashEmbeddings (no OPENAI_API_KEY). "
          "Set the key for real semantic retrieval.")
    return HashEmbeddings(dim=256)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def build_or_load_kb() -> VectorStore:
    """Load the KB from disk if we've indexed it before; otherwise walk
    ./data/kb/*.md and build it from scratch. Cached on disk so subsequent
    runs skip the embedding cost."""
    if KB_INDEX_PATH.exists():
        return VectorStore.load(KB_INDEX_PATH, embeddings=_embeddings())

    if not KB_DIR.exists():
        print(f"[kb] no {KB_DIR}/ found -- seeding with a tiny sample")
        KB_DIR.mkdir(parents=True, exist_ok=True)
        (KB_DIR / "refunds.md").write_text(
            "# Refund policy\n\n"
            "Subscriptions can be refunded within 30 days of purchase. "
            "Prorated refunds are not available. Contact billing@example.com."
        )
        (KB_DIR / "retention.md").write_text(
            "# Data retention\n\n"
            "User data is retained for 90 days after account cancellation. "
            "After that, all personal data is purged from our systems."
        )
        (KB_DIR / "auth.md").write_text(
            "# Authentication\n\n"
            "API keys are issued from the dashboard. Each key has a scope "
            "and can be revoked at any time. Bearer-token auth is standard."
        )

    store = VectorStore(embeddings=_embeddings())
    for f in KB_DIR.rglob("*.md"):
        text = f.read_text(encoding="utf-8")
        chunks, i = [], 0
        while i < len(text):
            chunks.append(text[i: i + 1000])
            i += 800   # 200-char overlap
        store.add(
            chunks,
            metadata=[
                {"source": str(f.relative_to(KB_DIR)), "chunk": j}
                for j in range(len(chunks))
            ],
        )
    KB_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.save(KB_INDEX_PATH)
    print(f"[kb] indexed {len(store)} chunks from {KB_DIR}/")
    return store


# ---------------------------------------------------------------------------
# Per-user notes store
# ---------------------------------------------------------------------------

def user_notes_store(user_id: str) -> VectorStore:
    path = USERS_DIR / f"{user_id}.json"
    if path.exists():
        return VectorStore.load(path, embeddings=_embeddings())
    return VectorStore(embeddings=_embeddings())


def index_user_facts(user_id: str, user_msg: str, assistant_msg: str) -> None:
    """Append the turn into the user's notes store so future
    user_notes_search calls can retrieve it."""
    notes = user_notes_store(user_id)
    notes.add(
        [f"USER SAID: {user_msg}", f"ASSISTANT REPLIED: {assistant_msg}"],
        metadata=[
            {"kind": "user_turn", "ts": _now()},
            {"kind": "assistant_turn", "ts": _now()},
        ],
    )
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    notes.save(USERS_DIR / f"{user_id}.json")


# ---------------------------------------------------------------------------
# System prompt -- the product
# ---------------------------------------------------------------------------

SYSTEM = """You are a careful research assistant.

For EVERY question, follow this process:

1. Decompose the question into 2-5 focused sub-queries. Do NOT search
   for the raw user question; break it apart first.
2. Call `kb_search` MULTIPLE times in the same turn -- one per sub-query.
   Also call `user_notes_search` if the question might depend on prior
   context ("my dog", "the project I mentioned", "last time we spoke").
3. Read the returned chunks. If NONE score above 0.35, re-query with
   different phrasing OR say clearly: "I don't have that in my sources."
4. Compose the answer using ONLY facts explicitly present in the
   chunks. Never fill in gaps from general knowledge.
5. End every answer with a `Sources:` line listing the retrieved
   passages you used, formatted `[source: <path> chunk <N>]`.

If the question is a follow-up ("what about the second one?"), resolve
the referent from user_notes_search first."""


# ---------------------------------------------------------------------------
# Build the agent
# ---------------------------------------------------------------------------

def build_agent(user_id: str) -> AgentRunner:
    if os.getenv("ANTHROPIC_API_KEY"):
        llm = Claude(
            model="claude-sonnet-4-6",
            enable_prompt_cache=True,
            cache_history_after=6,
        ).configure_limits(
            budget_usd=1.00,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        )
    else:
        llm = GPT(model="gpt-4o").configure_limits(
            budget_usd=1.00,
            input_price_per_1k=0.005,
            output_price_per_1k=0.015,
        )

    kb = build_or_load_kb()
    notes = user_notes_store(user_id)

    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[
            vector_search_tool(
                kb, name="kb_search", default_top_k=4, max_top_k=8,
                description=(
                    "Search the PUBLIC knowledge base (product docs, "
                    "policies, tutorials). Use for questions about how "
                    "things work, definitions, or company policy. Call "
                    "MULTIPLE times in one turn with different sub-queries "
                    "when the question is complex."
                ),
            ),
            vector_search_tool(
                notes, name="user_notes_search",
                default_top_k=3, max_top_k=6,
                description=(
                    "Search THIS USER's private notes and prior conversation "
                    "history. Use when the question references 'my', 'the "
                    "one I mentioned', 'last time', or when you need "
                    "personal context to disambiguate."
                ),
            ),
        ],
        bind_tools_natively=True,
        parallel_tool_workers=6,
        max_iterations=6,
        verbose=False,
        system_addendum=SYSTEM,
    )


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------

def chat(user_id: str, user_message: str) -> str:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_path = SESSIONS_DIR / f"{user_id}.json"

    runner = build_agent(user_id)

    session = (
        Session.load(session_path).attach(runner) if session_path.exists()
        else Session.start(runner, metadata={"user_id": user_id})
    )

    result = session.invoke(user_message)
    session.save(session_path)

    index_user_facts(user_id, user_message, result.content)
    return result.content


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Agentic RAG chatbot demo")
    ap.add_argument("query", nargs="*", help="user question")
    ap.add_argument("--user", default="anon", help="user id for session + notes")
    ap.add_argument("--demo", action="store_true",
                    help="run a scripted 3-turn session showing memory")
    args = ap.parse_args()

    if args.demo:
        print("\n>>> Turn 1: introduces personal context")
        print(chat(args.user, "My favorite feature is the sandbox permissions."))
        print("\n>>> Turn 2: policy question")
        print(chat(args.user, "How long do you retain user data after cancellation?"))
        print("\n>>> Turn 3: follow-up that requires user_notes recall")
        print(chat(args.user, "Remind me which feature I said I liked?"))
        return

    if not args.query:
        ap.print_help()
        return

    print(chat(args.user, " ".join(args.query)))


if __name__ == "__main__":
    main()
