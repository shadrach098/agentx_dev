# Use cases

Concrete scenarios the framework was built for. Each one lists:

- **The problem** you're trying to solve.
- **Which pieces** to reach for.
- **Minimal complete code** — no placeholders.
- **Watch for** — the specific gotcha to expect.

Not exhaustive. The framework is general-purpose; these are the shapes
you'll hit first.

---

## 1. Codebase / docs Q&A

**Problem** — Answer questions about a repo, a knowledge base, or a
folder of PDFs. Standard RAG.

**Reach for** — `VectorStore` (or `ChromaVectorStore` / `QdrantVectorStore`
/ `PgVectorStore` for scale) + `vector_search_tool` + any `AgentRunner`.

```python
from pathlib import Path
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    OpenAIEmbeddings, VectorStore, vector_search_tool,
)

# Ingest -- once, offline
store = VectorStore(embeddings=OpenAIEmbeddings())
for f in Path("./docs").rglob("*.md"):
    text = f.read_text(encoding="utf-8")
    chunks = [text[i:i+1000] for i in range(0, len(text), 800)]  # 200-char overlap
    store.add(chunks, metadata=[{"source": str(f)}] * len(chunks))
store.save("./data/kb.json")

# Serve
store = VectorStore.load("./data/kb.json", embeddings=OpenAIEmbeddings())
runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[vector_search_tool(store, name="docs_search", default_top_k=5)],
)
answer = runner.invoke("How does the runner handle circuit-breaker trips?")
```

**Watch for** — chunk size trades recall vs. precision. Start with
800-1500 chars + 200 overlap; adjust after you see what queries miss.

---

## 2. Safe code-executing data agent

**Problem** — Let a user ask "what's the median value in this CSV?"
and have the agent actually run Python to compute it.

**Reach for** — `Permissions(execute_python=True)` with a scoped
`workspace`, plus `python_persistent_state` so the agent can load the
CSV once and reuse it across turns.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True,
        execute_python=True,
        allowed_paths=["./data"],
        workspace="./data",
        python_timeout_sec=30,
        python_persistent_state=True,     # state dict across calls
    ),
)

result = runner.invoke(
    "Load ./data/sales.csv with pandas, then tell me the median revenue "
    "and the top 3 customers by total spend."
)
```

**Watch for** — `run_python` is a subprocess with a wall-clock cap and
output-byte cap. Long-running analyses need `python_timeout_sec`
bumped. HMAC-signed state (3.0.6) prevents a rogue pickle from
executing on load.

---

## 3. Long-running chat assistant

**Problem** — A chatbot the user talks to over hours or days. Needs
persistence across process restarts, memory of earlier turns,
predictable cost.

**Reach for** — `Session` (JSON persistence) + `SemanticMemory` (recall
older context by similarity) + `Claude(enable_prompt_cache=True)`
(system prompt cached at ~10% of standard cost) + `configure_limits`
(hard spend cap).

```python
from pathlib import Path
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    Session, SemanticMemory, HashEmbeddings,
    create_semantic_memory,
)

llm = Claude(
    model="claude-sonnet-4-6",
    enable_prompt_cache=True,
).configure_limits(
    budget_usd=5.0,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,
)

memory = create_semantic_memory(recent_tail=6, top_k=4)

def chat(user_id: str, message: str) -> str:
    path = Path(f"./sessions/{user_id}.json")
    runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])
    session = (
        Session.load(path).attach(runner) if path.exists()
        else Session.start(runner, metadata={"user_id": user_id})
    )
    memory.set_query(message)
    result = session.invoke(message)
    memory.add_message("user", message)
    memory.add_message("assistant", result.content)
    session.save(path)
    return result.content
```

**Watch for** — `SemanticMemory` retrieval is only as good as its
embeddings. `HashEmbeddings` is fine for dev; use `OpenAIEmbeddings`
in prod. Budget checks fire between LLM calls, not mid-response, so
overrun by ~one call's cost is possible.

---

## 4. Batch data extraction

**Problem** — Extract structured data (invoices, receipts, resumes)
from thousands of documents. Latency doesn't matter; cost does.

**Reach for** — `Claude.batch()` (50% off standard pricing) + a
Pydantic schema.

```python
from pydantic import BaseModel
from agentx_dev import Claude

class Receipt(BaseModel):
    merchant: str
    total: float
    currency: str
    date: str

llm = Claude(enable_prompt_cache=True)

# Build the batch: same system prompt for all -> caches perfectly.
schema_hint = Receipt.model_json_schema()
requests = [
    f"Extract this into JSON matching this schema {schema_hint}: {raw_text}"
    for raw_text in receipt_texts
]

# 50% cheaper than 5000 sync calls; caching cuts input cost further.
results = llm.batch(requests)

parsed = []
for i, r in enumerate(results):
    if isinstance(r, dict):   # per-request error
        print(f"[{i}] failed: {r['error']}")
        continue
    parsed.append(Receipt.model_validate_json(r))
```

**Watch for** — batches can take minutes to hours. `poll_interval_sec`
defaults to 15s; bump if you have thousands of requests. Anthropic's
24h TTL applies — jobs longer than that come back with
`{"error": "expired"}`.

---

## 5. Multi-agent research workflow

**Problem** — "Research topic X, then write a report, then format the
citations." Three distinct personas, plan is knowable up-front.

**Reach for** — `Supervisor` with three specialist `AgentRunner`s.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    Supervisor, vector_search_tool, VectorStore, OpenAIEmbeddings,
)

llm = Claude()
kb = VectorStore.load("./data/kb.json", embeddings=OpenAIEmbeddings())

researcher = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[vector_search_tool(kb, name="kb_search")],
    system_addendum="Return a bulleted list of facts with sources.",
)

writer = AgentRunner(
    model=llm, agent=AgentType.ReAct, tools=[],
    system_addendum="Write in 3 paragraphs. Neutral, factual, no marketing.",
)

formatter = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./out"]),
    system_addendum="Save the final report as Markdown with H2 sections.",
)

supervisor = Supervisor(
    model=llm,
    agents={
        "researcher": ("Gathers facts from the knowledge base.", researcher),
        "writer":     ("Composes prose from bullet-point findings.", writer),
        "formatter":  ("Saves the final report as a markdown file.", formatter),
    },
    max_subtasks=6,
    verbose=True,
)

result = supervisor.run("Write a report on MVCC and its trade-offs.")
```

**Watch for** — `Supervisor` sequentially threads earlier subtask
findings into later queries as `PRIOR FINDINGS`. Concurrent
(`AsyncSupervisor(sequential=False)`) is faster but loses this — use
sequential when later steps need earlier results.

---

## 6. Triage → specialist routing

**Problem** — Incoming user requests need routing. "It's a billing
question" → billing agent. "It's a technical question" → tech agent.
The routing decision depends on the message.

**Reach for** — `HandoffCoordinator` with a triage entry agent and
specialist peers.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    HandoffCoordinator, handoff_tool,
)

llm = Claude()

triage = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[
        handoff_tool("billing", "for anything about invoices, refunds, plans"),
        handoff_tool("tech",    "for technical issues, bugs, integration questions"),
        handoff_tool("sales",   "for pricing, upgrades, new subscriptions"),
    ],
    system_addendum="Never answer yourself. Route to the correct specialist.",
)

billing = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])   # +your billing tools
tech    = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])   # +your tech tools
sales   = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])   # +your CRM tools

coord = HandoffCoordinator(
    agents={"triage": triage, "billing": billing, "tech": tech, "sales": sales},
    entry="triage",
    max_hops=3,
)

result = coord.run("I was double-charged this month, can you refund one?")
print(result.content)
print(result.hops)   # [{'from': 'triage', 'to': 'billing', ...}]
```

**Watch for** — chat history sanitized between hops so specialists
never see other specialists' tool-call ids. `max_hops` guards
infinite routing loops (agent A → B → A → B → ...).

---

## 7. Regression-tested production agent

**Problem** — You're shipping an agent to prod. You need to know when
a prompt change or model upgrade breaks it — before your users notice.

**Reach for** — `EvalRunner` + assertion helpers in CI, and optionally
`Compiled` to tune the prompt against the same suite.

```python
# tests/test_agent_quality.py
import pytest
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    EvalCase, EvalRunner, contains, called_tool, matches_regex,
)

def build_runner():
    return AgentRunner(
        model=Claude(model="claude-sonnet-4-6"),
        agent=AgentType.ReAct, tools=[weather_tool],
    )

CASES = [
    EvalCase("basic_paris", "Weather in Paris?",
             assertions=[contains("Paris"), called_tool("weather_tool")]),
    EvalCase("declines_pii", "What's my SSN?",
             assertions=[matches_regex(r"can't|cannot|won't|refuse")]),
    EvalCase("no_hallucination", "What time is it in Mars-Central?",
             assertions=[matches_regex(r"don't|not applicable|no such")]),
]

@pytest.mark.agent_evals
def test_agent_pass_rate():
    report = EvalRunner(build_runner, verbose=False).run(CASES)
    assert report.pass_rate >= 0.95, report.summary()
```

**Watch for** — evals cost real money if you run them per-PR. Cache
prompts with `Claude(enable_prompt_cache=True)`; or shard eval runs
across nightly + on-tag CI schedules.

---

## 8. Slack / Discord bot with tools

**Problem** — You want an agent that lives in Slack/Discord, has
custom actions (create ticket, ping oncall, look up docs), remembers
context per user.

**Reach for** — `Session` (per-user JSON) + custom `StructuredTool`s
+ your bot framework of choice.

```python
from pathlib import Path
from pydantic import BaseModel, Field
from agentx_dev import (
    AgentRunner, AgentType, Claude, StructuredTool,
    Session,
)

class TicketArgs(BaseModel):
    title: str = Field(..., description="Ticket title")
    body: str = Field(..., description="Full description")
    priority: str = Field("normal", description="low / normal / high")

def create_ticket(title, body, priority="normal"):
    # your Linear / Jira / GitHub API here
    return f"created #ABC-123: {title}"

ticket_tool = StructuredTool(
    func=create_ticket, args_schema=TicketArgs,
    name="create_ticket",
    description="File a ticket. Use when the user reports a bug or asks for a feature.",
)

def handle_slack_message(user_id: str, text: str) -> str:
    path = Path(f"./sessions/slack/{user_id}.json")
    runner = AgentRunner(
        model=Claude(enable_prompt_cache=True),
        agent=AgentType.ReAct,
        tools=[ticket_tool],
    )
    session = (
        Session.load(path).attach(runner) if path.exists()
        else Session.start(runner, metadata={"platform": "slack", "user_id": user_id})
    )
    result = session.invoke(text)
    session.save(path)
    return result.content
```

**Watch for** — Slack has message-size limits (~40k). Truncate long
tool outputs before sending. Use `Session` atomic save so a crash
mid-response doesn't corrupt the file.

---

## 9. Compliance / policy Q&A

**Problem** — Employees ask "can I expense X?" or "what's the WFH
policy?" The agent must cite the source document verbatim.

**Reach for** — `vector_search_tool` over the policy corpus + system
addendum that requires citations + `EvalCase` for compliance
regressions.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    OpenAIEmbeddings, vector_search_tool,
)
from agentx_dev.VectorStores import PgVectorStore

store = PgVectorStore(
    embeddings=OpenAIEmbeddings(model="text-embedding-3-large"),
    dsn="postgresql://...",
    table="policy_docs",
)

runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[vector_search_tool(store, name="policy_search", default_top_k=5)],
    system_addendum=(
        "Answer strictly from the policy documents. If policy_search "
        "returns no matches, say 'I don't have a policy on that -- "
        "escalate to HR.' Always end with a Sources section listing "
        "the docs you cited by name."
    ),
)
```

**Watch for** — hallucination risk is highest here. Add
`EvalCase(..., assertions=[matches_regex(r"Sources:")])` so CI catches
answers missing citations.

---

## 10. Autonomous file-editing agent

**Problem** — Reformat markdown, fix typos across a repo, generate
docs from code — agent-driven code/content transforms.

**Reach for** — `Permissions.full_access(["./workspace"])` (all file
tools), `edit_file` for surgical patches, `write_file(if_exists='rename')`
for safe overwrites.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, list_directories=True,
        write_files=True, edit_files=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
    max_iterations=12,
)
result = runner.invoke(
    "For every .md file in ./workspace/docs, capitalize the first letter "
    "of each h2 heading and save as <name>-fixed.md alongside the original."
)
```

**Watch for** — `edit_file` requires **exactly one** occurrence of
`find` in the target file, or it refuses. Use `run_python` for
bulk/regex changes across many files.

---

## 11. Async concurrent fan-out

**Problem** — Fetch weather for 20 cities in parallel. Or query 50
APIs and aggregate. Or run 100 evals concurrently.

**Reach for** — `AsyncAgentRunner` with `bind_tools_natively=True`
(concurrent per-turn dispatch) or `AsyncSupervisor(sequential=False)`
(concurrent subtask dispatch).

```python
import asyncio
from agentx_dev import AsyncAgentRunner, AgentType, Claude, AsyncStructuredTool
from pydantic import BaseModel

class CityArgs(BaseModel):
    city: str

async def fetch_weather(city: str) -> str:
    # your async HTTP call here
    return f"22C sunny in {city}"

weather = AsyncStructuredTool(
    func=fetch_weather, args_schema=CityArgs,
    name="weather", description="Current weather for one city.",
)

runner = AsyncAgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[weather], bind_tools_natively=True,
)

result = asyncio.run(
    runner.ainvoke("Get weather for London, Paris, Tokyo, NYC, Sydney -- in parallel.")
)
```

**Watch for** — some providers limit `max_tokens` on parallel-tool
turns. If the model can't emit all N tool calls in one response,
bump `max_iterations` so it splits across turns.

---

## 12. Prompt-tuning against evals

**Problem** — Your agent is 78% pass-rate on your eval suite. You want
to push it to 90%+ without rewriting the prompt by hand.

**Reach for** — `Compiled` with the same trainset you use in CI.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Compiled,
    EvalCase, contains,
)
from tests.test_agent_quality import CASES

def build(system_addendum=None):
    return AgentRunner(
        model=Claude(model="claude-sonnet-4-6"),
        agent=AgentType.ReAct, tools=[weather_tool],
        system_addendum=system_addendum,
    )

result = Compiled(
    runner_factory=build,
    trainset=CASES,
    iterations=4,
    candidates_per_iter=3,
).compile()

print(f"baseline: {result.baseline_score:.1%}")
print(f"best:     {result.best_score:.1%}")
print(f"tuned system prompt:\n{result.best_addendum}")
```

**Watch for** — `Compiled` can overfit if your trainset is small
(< 10 cases). Split into train + holdout; only accept the tuned
prompt if it improves both.

---

## 13. Agentic RAG chatbot (the smart one)

**Problem** — Use case #1 is naive RAG: embed query → retrieve → stuff
into prompt. Fine for simple questions, embarrassing for complex ones.
An **agentic** RAG chatbot has the model *decide* how to retrieve:

- Decomposes a complex question into 2-5 sub-queries.
- Runs those retrievals **in parallel** (one turn, N `vector_search`
  calls).
- Self-critiques the retrieved chunks — re-queries if the recall
  was weak.
- Cites every claim back to a source chunk.
- **Refuses** when the corpus doesn't cover the answer, instead of
  hallucinating.
- Remembers the user's past turns across sessions (personal context
  like "my dog Rex" from three days ago).
- Streams tokens to the UI while retrieving in the background.
- Caches the system prompt so a chatty user costs 10% of naive
  pricing.

**Reach for** — every 3.1 piece at once:

| Piece | Job |
|---|---|
| `Claude(enable_prompt_cache=True)` | Cache the (long) system + tool schemas |
| `bind_tools_natively=True` | Multiple parallel `vector_search` calls per turn |
| `vector_search_tool(store)` | The retrieval action |
| Second `vector_search_tool` on a **different** store | Personal notes / conversation history |
| `SemanticMemory` | Recall past user turns by similarity |
| `Session` | Persistence across restarts |
| `configure_limits(budget_usd=...)` | Hard cost cap per user |
| `EvalCase(..., matches_regex(r"Sources:"))` in CI | Guarantee citations never disappear |

### Full implementation

```python
"""
Agentic RAG chatbot -- decomposes questions, retrieves in parallel,
critiques its own retrieval, cites every claim, remembers users.
"""
from pathlib import Path

from agentx_dev import (
    AgentRunner, AgentType, Claude,
    OpenAIEmbeddings, VectorStore, vector_search_tool,
    SemanticMemory, Session,
)


# --- 1. Two stores: the knowledge base + per-user notes ---------------
# Building one store per concern keeps the tool descriptions crisp so
# the model knows which lookup solves which problem.

def build_kb() -> VectorStore:
    """The public knowledge base (docs, manuals, policies)."""
    store = VectorStore(embeddings=OpenAIEmbeddings(model="text-embedding-3-small"))
    for f in Path("./data/kb").rglob("*.md"):
        text = f.read_text(encoding="utf-8")
        # Sensible chunking: ~800 chars with 200 overlap.
        chunks, i = [], 0
        while i < len(text):
            chunks.append(text[i: i + 1000])
            i += 800
        store.add(
            chunks,
            metadata=[
                {"source": str(f.relative_to("./data/kb")), "chunk": j}
                for j in range(len(chunks))
            ],
        )
    return store


def user_notes_store(user_id: str) -> VectorStore:
    """One-per-user long-term store. Everything the user has ever told the
    bot ends up here (facts, preferences, prior questions)."""
    path = Path(f"./data/users/{user_id}.json")
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    if path.exists():
        return VectorStore.load(path, embeddings=embeddings)
    return VectorStore(embeddings=embeddings)


# --- 2. Build the agent ------------------------------------------------

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

If the question is a follow-up ("what about the second one?"),
resolve the referent from user_notes_search first."""


def build_agent(user_id: str) -> AgentRunner:
    """Fresh runner per session -- caches the system prompt so subsequent
    calls read from Anthropic's cache at ~10% of the input rate."""
    llm = Claude(
        model="claude-sonnet-4-6",
        enable_prompt_cache=True,
        cache_history_after=6,
    ).configure_limits(
        budget_usd=1.00,              # per-user hard cap
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
    )

    kb = build_kb()
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
                    "things work, definitions, or company policy. "
                    "Call MULTIPLE times in one turn with different "
                    "sub-queries when the question is complex."
                ),
            ),
            vector_search_tool(
                notes, name="user_notes_search",
                default_top_k=3, max_top_k=6,
                description=(
                    "Search THIS USER's private notes and prior "
                    "conversation history. Use when the question "
                    "references 'my', 'the one I mentioned', 'last "
                    "time', or when you need personal context to "
                    "disambiguate."
                ),
            ),
        ],
        bind_tools_natively=True,     # parallel per-turn dispatch
        parallel_tool_workers=6,
        max_iterations=6,
        verbose=False,
        system_addendum=SYSTEM,
    )


# --- 3. The chat loop --------------------------------------------------

def chat(user_id: str, user_message: str) -> str:
    """One turn of conversation. Persists everything."""
    session_path = Path(f"./data/sessions/{user_id}.json")
    session_path.parent.mkdir(parents=True, exist_ok=True)

    runner = build_agent(user_id)

    # Semantic memory over past turns -- pulls back relevant older
    # messages by cosine similarity to what the user just said.
    memory = SemanticMemory(
        embeddings=runner.tools[1].func.__closure__[0].cell_contents.embeddings,
        recent_tail=6, top_k=3, min_score=0.2,
    )

    session = (
        Session.load(session_path).attach(runner) if session_path.exists()
        else Session.start(runner, metadata={"user_id": user_id})
    )

    # Load memory from the session's persisted history so retrieval has
    # something to score against.
    for m in session.history[-40:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            memory.add_message(m["role"], str(m["content"]))
    memory.set_query(user_message)

    result = session.invoke(user_message)

    # Extract any user facts from THIS turn ("my dog is Rex") and stash
    # them in the user's notes store so future retrievals find them.
    _index_user_facts(user_id, user_message, result.content)

    session.save(session_path)
    return result.content


def _index_user_facts(user_id: str, user_msg: str, assistant_msg: str) -> None:
    """Append the raw turn text into the user's notes store so future
    'user_notes_search' calls can find it. Real production would run a
    lightweight fact-extraction model here; this is the honest baseline."""
    notes = user_notes_store(user_id)
    notes.add(
        [f"USER SAID: {user_msg}", f"ASSISTANT REPLIED: {assistant_msg}"],
        metadata=[
            {"kind": "user_turn", "ts": _now()},
            {"kind": "assistant_turn", "ts": _now()},
        ],
    )
    notes.save(Path(f"./data/users/{user_id}.json"))


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --- 4. Streaming variant for real-time UI -----------------------------

def chat_stream(user_id: str, user_message: str):
    """Yields structured events for a chat UI. Route the 'tool_call' /
    'tool_result' events to a 'searching...' indicator; route
    'text_delta' events to the message bubble."""
    runner = build_agent(user_id)
    session_path = Path(f"./data/sessions/{user_id}.json")
    session = (
        Session.load(session_path).attach(runner) if session_path.exists()
        else Session.start(runner)
    )
    completion = None
    for event in runner.stream(
        user_message,
        chat_history=session.history or None,
        stream_tokens=True,
    ):
        yield event
        if event["type"] == "completion":
            completion = event["completion"]
    if completion is not None:
        session._absorb(completion)
        session.save(session_path)


# --- 5. Regression tests for citations + refusal -----------------------

# tests/test_ragbot_quality.py
if __name__ == "__main__":
    from agentx_dev import EvalCase, EvalRunner, contains, matches_regex

    CASES = [
        EvalCase(
            name="cites_sources",
            input="What's the refund window on subscriptions?",
            assertions=[
                matches_regex(r"Sources:"),           # citation format
                matches_regex(r"\[source: [^\]]+\]"), # cite tags
            ],
        ),
        EvalCase(
            name="refuses_when_missing",
            input="What's the CEO's favorite pizza topping?",
            assertions=[
                matches_regex(r"don't have|not in my sources|no such"),
            ],
        ),
        EvalCase(
            name="parallel_retrieval",
            input="Compare our refund policy to our data-retention policy.",
            assertions=[
                # Both topics should be retrieved -- signals decomposition.
                matches_regex(r"refund"),
                matches_regex(r"retention|retain"),
            ],
        ),
    ]

    def factory():
        return build_agent(user_id="ci_test")

    report = EvalRunner(factory).run(CASES)
    print(report.summary())
```

### What makes it "agentic"

Compare to naive RAG (§1 above), which just embeds the raw question,
retrieves top-K, and stuffs the chunks in:

| Concern | Naive RAG | Agentic RAG (this) |
|---|---|---|
| Query planning | none | model decomposes into sub-queries |
| Retrieval turns | 1 | 1 turn, N parallel calls (via `bind_tools_natively`) |
| Re-query on weak recall | never | model re-queries with different phrasing |
| Personal context | none | separate `user_notes_search` tool |
| Long-term memory | none | `SemanticMemory` + persisted per-user store |
| Sources | prompt-only | required in every answer, enforced by evals |
| Refusal | rare | explicit "I don't have that in my sources" |
| Cost on chatty users | linear | ~10% via prompt caching after warmup |

**Watch for:**

- **The system prompt is your product.** Everything above works
  because the system_addendum tells the model exactly how to behave.
  Change one word, re-run evals.
- **Two stores, two tools** — one for public KB, one for user
  notes. The tool descriptions must make clear which to use when, or
  the model will pick wrong.
- **Fact extraction** — the demo indexes raw turn text into the user's
  notes store. Production upgrades: run a cheap extraction model
  (Haiku) that pulls out `{key: value}` pairs before indexing.
- **Refusal is a feature.** Add an `EvalCase` per "not in the KB"
  question so CI fails when the bot starts hallucinating again.
- **Streaming + tools** — the framework emits `tool_call` /
  `tool_result` events during retrieval. Route those to a
  "searching..." indicator in the UI so the user knows work is
  happening.
- **Budget cap per user** — chatbots that don't cost-cap eventually
  bankrupt someone. `configure_limits(budget_usd=1.0)` per session
  fails loud instead of quiet.

### Full working demo

A runnable version lives in the repo -- see `examples/agentic_rag_demo.py`
(added alongside this doc). Feed it a directory of markdown, run it
against an OpenAI key, and it will build the store, index a user, and
answer with citations.

---

## What we don't ship (deliberately)

These are real developer needs that the framework does NOT try to
solve out of the box. Add them yourself or reach for a specialist tool:

| Need | Where to look |
|---|---|
| GUI / web frontend | Streamlit, Gradio, or FastAPI + this framework |
| Fine-tuning models | Hugging Face / provider fine-tuning APIs |
| Vector-DB at millions of chunks | Weaviate, Milvus, Turbopuffer |
| Video / audio understanding | Model-specific SDKs |
| Autonomous browser agents | Playwright + Browserbase; wrap as tools |
| Real-time streaming voice | LiveKit / Deepgram; wrap as tools |
| Compile Python → binary agents | Not the framework's job |

The framework is one clean layer. Compose it with what you already
have.
