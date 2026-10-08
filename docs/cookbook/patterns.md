# Patterns cookbook

Reusable shapes for common problems.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## 1. Single tool-using agent

Just build a runner with your tools.

```python
runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[my_tool])
runner.invoke("...")
```

## 2. File-editing agent (sandboxed)

DefaultTools with permissions.

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
)
```

See [file-agent guide](../guides/file-agent.md).

## 3. Extraction pipeline (no agent loop)

`with_structured_output` for one-shot.

```python
class Ticket(BaseModel):
    title: str
    priority: Literal["low", "med", "high"]

extractor = Claude().with_structured_output(Ticket)
ticket = extractor.invoke(raw_email_body)
```

## 4. Chatbot with persistence

`Session` + a runner factory.

```python
def chat(user_id: str, message: str) -> str:
    path = Path(f"./sessions/{user_id}.json")
    runner = build_runner()
    session = (
        Session.load(path).attach(runner) if path.exists()
        else Session.start(runner)
    )
    result = session.invoke(message)
    session.save(path)
    return result.content
```

## 5. RAG chatbot

Vector store + `vector_search_tool` + semantic memory.

```python
store = VectorStore(embeddings=OpenAIEmbeddings())
store.add(chunks_from("./docs"))

memory = SemanticMemory(embeddings=store.embeddings, top_k=4)

runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[vector_search_tool(store)],
)

def ask(question):
    memory.set_query(question)
    result = runner.invoke(question, chat_history=memory.get_messages())
    memory.add_message("user", question)
    memory.add_message("assistant", result.content)
    return result.content
```

## 6. Triage → specialists via handoffs

```python
triage = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[
    handoff_tool("researcher"), handoff_tool("writer"),
])
researcher = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[
    vector_search_tool(kb), handoff_tool("writer"),
])
writer = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])

coord = HandoffCoordinator(
    {"triage": triage, "researcher": researcher, "writer": writer},
    entry="triage",
)
result = coord.run("Draft an intro to MVCC.")
```

## 7. Plan → dispatch → synthesize via Supervisor

```python
supervisor = Supervisor(
    model=llm,
    agents={
        "reader":   ("Reads files from ./data", reader_agent),
        "analyzer": ("Runs Python analysis", analyzer_agent),
        "writer":   ("Composes markdown reports", writer_agent),
    },
    max_subtasks=6,
)
result = supervisor.run("Read the CSVs in ./data, compute quarterly totals, write a report.")
```

## 8. Fan-out then aggregate (async)

Concurrent sub-tasks with `AsyncSupervisor(sequential=False)`.

```python
result = await AsyncSupervisor(
    model=llm, agents=agents, sequential=False,
).run("For each city in [NYC, LA, SF], fetch weather and news.")
```

## 9. Human-in-the-loop tool approval

Wrap the dispatch layer:

```python
def approve(tool_name, args) -> bool:
    print(f"Agent wants to call {tool_name} with {args}")
    return input("y/n? ").lower() == "y"

original_dispatch = runner.registry.dispatch
def gated_dispatch(name, args):
    if not approve(name, args):
        return ToolError(f"user rejected call to {name}", tool=name)
    return original_dispatch(name, args)

runner.registry.dispatch = gated_dispatch
```

For Supervisor spawn approval, use `SpawnConfig(approver=...)`.

## 10. Multi-provider fallback

Try Claude first; fall back to GPT on error.

```python
class Fallback(BaseChatModel):
    def __init__(self, primary, secondary):
        self.p, self.s = primary, secondary
    def Initialize(self, messages):
        try: return self.p.Initialize(messages)
        except Exception:
            return self.s.Initialize(messages)

runner = AgentRunner(model=Fallback(Claude(), GPT()), agent=AgentType.ReAct)
```

## 11. Cost-capped batch runner

```python
llm = Claude().configure_limits(budget_usd=1.0, input_price_per_1k=0.003, output_price_per_1k=0.015)

for input in inputs:
    try:
        result = runner.invoke(input)
        save(result)
    except CostBudgetExceeded as e:
        print(f"Stopping at ${e.spent_usd:.2f} — budget exceeded")
        break
```

## 12. Streaming to a web client

```python
@app.post("/chat")
async def chat(req: dict):
    async def event_stream():
        # astream() yields step events only -- it takes no stream_tokens.
        async for event in runner.astream(req["query"]):
            if event["type"] == "completion": continue
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

For token-level deltas the loop has to be the sync runner
(`runner.stream(..., stream_tokens=True)` with
`use_function_calling=False`); `astream` has no `text_delta` path.

## 13. Evals in CI

```python
# tests/test_agent.py
import pytest
from agentx_dev import EvalCase, EvalRunner, contains

@pytest.mark.evals
def test_agent_quality():
    cases = [EvalCase(name="X", input="...", assertions=[contains("Y")])]
    report = EvalRunner(build_runner).run(cases)
    assert report.pass_rate == 1.0, report.summary()
```

## 14. Tool retry with different strategy

Some tools benefit from retry with backoff at the tool level (rather
than the model level). Wrap the func:

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def flaky_api_call(query: str) -> str:
    ...
```

Then use it in a `StructuredTool` as normal.

## 15. Two-agent debate

Have two agents critique each other's answers via handoffs; bound with
`max_hops`.

```python
answerer = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[handoff_tool("critic")])
critic = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[handoff_tool("answerer")])

coord = HandoffCoordinator(
    {"answerer": answerer, "critic": critic},
    entry="answerer", max_hops=4,
)
```

The answerer proposes; critic finds flaws; answerer revises; critic
approves. `max_hops=4` = at most 2 revision rounds.

## 16. Caching some tools but not others

`registry.configure_cache()` is **all-or-nothing** — it attaches one
cache that every dispatch reads and writes through, keyed on
`(tool_name, args)`. There is no per-tool opt-out at the registry
level, so a time-sensitive tool like `current_time` will happily serve
you a stale answer.

When only *some* tools should cache, leave the registry cache off and
decorate the individual tool functions instead:

```python
from agentx_dev import AgentRunner, StandardTool, InMemoryCache, cached_tool

cache = InMemoryCache(default_ttl=300)

@cached_tool(cache, ttl=3600)          # stable, expensive -> cache hard
def lookup_company(ticker: str) -> str:
    return expensive_api_call(ticker)

def current_time(_: str) -> str:       # never decorated -> never cached
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

runner = AgentRunner(
    model=llm,
    tools=[
        StandardTool(func=lookup_company, description="Look up a company by ticker."),
        StandardTool(func=current_time, description="Current UTC time."),
    ],
)
# Note: no runner.registry.configure_cache(...) call.
```

Use the registry-wide cache (`configure_cache(get_global_cache())`)
only when *every* tool in that runner is safe to memoize — a pure
retrieval agent, say. Mixing the two means the registry cache wins at
dispatch and your per-tool TTLs stop mattering.

## 17. Confidential redaction on outputs

Route all completions through a redactor before returning to users:

```python
from agentx_dev import redact_secrets

def safe_invoke(runner, query):
    result = runner.invoke(query)
    result.content = redact_secrets(result.content)
    return result
```

## 18. Agentic RAG with parallel retrieval *(3.1)*

Instead of naive "embed query → retrieve top-K → answer," let the model
decompose the question into sub-queries and dispatch them concurrently:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    OpenAIEmbeddings, VectorStore, vector_search_tool,
)

store = VectorStore.load("./data/kb.json", embeddings=OpenAIEmbeddings())

runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[vector_search_tool(store, name="kb_search", default_top_k=4)],
    bind_tools_natively=True,      # parallel per-turn dispatch
    parallel_tool_workers=6,
    system_addendum=(
        "Decompose the question into 2-5 sub-queries. Call kb_search "
        "MULTIPLE times in one turn -- one per sub-query. End every "
        "answer with a Sources line listing the passages you used."
    ),
)
result = runner.invoke("Compare our refund and retention policies.")
```

See use case §13 for the full agentic RAG chatbot including
user-notes memory and citation-enforced evals.

## 19. Batch data extraction at 50% off *(3.1)*

For embarrassingly-parallel workloads (labeling, extraction,
classification) where latency doesn't matter, use the Anthropic batch
endpoint:

```python
from pydantic import BaseModel
from agentx_dev import Claude

class Receipt(BaseModel):
    merchant: str
    total: float
    currency: str

llm = Claude(enable_prompt_cache=True)   # or GPT() -- same API
schema = Receipt.model_json_schema()

requests = [
    f"Extract this into JSON matching {schema}: {raw}"
    for raw in receipt_texts   # potentially thousands
]

results = llm.batch(requests, poll_interval_sec=30)

for i, r in enumerate(results):
    if isinstance(r, dict):        # per-request failure
        print(f"[{i}] {r['type']}: {r['error']}")
        continue
    receipt = Receipt.model_validate_json(r)
```

50% cheaper than sync `.invoke()`. Prompt caching cuts input cost
further because every request shares the same schema in the prompt.

## 20. Compiled agent — prompt tuned against evals *(3.1)*

Wrap any runner factory in `Compiled` to iteratively improve its
`system_addendum` against your test suite:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    Compiled, EvalCase, contains, called_tool,
)

def build(system_addendum=None):
    return AgentRunner(
        model=Claude(), agent=AgentType.ReAct, tools=[weather_tool],
        system_addendum=system_addendum,
    )

trainset = [
    EvalCase("paris",  "Weather in Paris?",
             [called_tool("weather_tool"), contains("Paris")]),
    EvalCase("refuses", "What's my SSN?",
             [contains("can't")]),
]

result = Compiled(
    runner_factory=build,
    trainset=trainset,
    iterations=3,
    candidates_per_iter=3,
).compile()

# Deploy the tuned runner:
prod_runner_factory = lambda: build(system_addendum=result.best_addendum)
```

Pair with pattern §13 (`Evals in CI`) — use the same trainset for both
optimization and regression checks.

## 21. Streaming Supervisor / Handoffs to a web UI *(3.1)*

Both orchestrators emit structured events. Route them to Server-Sent
Events for a real-time UI:

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

@app.post("/supervisor")
async def stream_supervisor(request: dict):
    supervisor = build_supervisor()

    async def event_stream():
        for event in supervisor.stream(request["query"]):
            # Filter out the terminal completion -- it duplicates final
            if event["type"] == "completion":
                continue
            # SubtaskResult is a dataclass; serialize its shape
            if event["type"] == "subtask_result":
                event = {
                    "type": event["type"], "step": event["step"],
                    "result": {"agent": event["result"].agent,
                               "content": event["result"].content,
                               "error": event["result"].error},
                }
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Client-side: `EventSource("/supervisor")` and route each event.type to
a matching UI update. `plan` populates a task list; `dispatch` shows a
spinner on that task; `subtask_result` fills it in; `final` sets the
answer.

## 22. Chroma / Qdrant / pgvector — same code, different scale *(3.1)*

The vector-store adapters share the in-memory `VectorStore`'s public
shape, so you can start local and swap to a production DB with only
the store constructor:

```python
from agentx_dev import (
    HashEmbeddings, OpenAIEmbeddings, vector_search_tool,
)

# Dev / prototype (in-memory)
from agentx_dev import VectorStore
store = VectorStore(embeddings=HashEmbeddings())

# Small production (Chroma, persistent disk)
from agentx_dev.VectorStores import ChromaVectorStore
store = ChromaVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="prod_docs",
    persist_directory="./.chroma",
)

# Medium production (Qdrant, remote)
from agentx_dev.VectorStores import QdrantVectorStore
store = QdrantVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="prod_docs",
    url="https://<region>.qdrant.io:6333",
    api_key=os.environ["QDRANT_API_KEY"],
)

# Local file-backed Qdrant, no server (3.1.6) — use path=, NOT location=.
# location= is parsed as a hostname and a filesystem path there fails
# with an IDNA UnicodeError.
store = QdrantVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="prod_docs",
    path="./.qdrant",
)

# Already-have-Postgres production (pgvector)
from agentx_dev.VectorStores import PgVectorStore
store = PgVectorStore(
    embeddings=OpenAIEmbeddings(),
    dsn=os.environ["DATABASE_URL"],
    table="prod_docs",
)

# Same tool call regardless of backend:
tool = vector_search_tool(store, name="docs_search")
```

The rest of the code — `SemanticMemory`, `vector_search_tool`,
`runner.invoke` — never changes.

## 23. Self-hosted trace viewer during dev *(3.1)*

Turn on observability during dev, write to JSONL, and drop the file on
`viewer/index.html` to see the timeline:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    observability, FileHook, ConsoleHook, config,
)

config.observability_enabled = True
observability.add_hook(FileHook("./trace.jsonl"))   # for viewer/
observability.add_hook(ConsoleHook(verbose=False))  # optional stdout

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[...])
runner.invoke("...")

# Then: double-click viewer/index.html, drop trace.jsonl on it.
```

Wins over LangSmith when you need self-hosted, `file://`-friendly,
plain-text-JSONL-archivable debugging.

## 24. Typed RAG pipeline: intent → retrieve → rerank *(3.2)*

Specialists declare Pydantic output schemas once; the Supervisor passes
validated instances between them instead of prose. Downstream steps
parse fields, not sentences.

```python
from pydantic import BaseModel, Field
from agentx_dev import AgentRunner, AgentType, Claude, Supervisor
from agentx_dev.Tools import vector_search_tool

class QueryIntent(BaseModel):
    intent: str
    entity: str = ""
    search_query: str = Field(..., description="Optimized semantic search query.")
    needs_rag: bool
    confidence: float

class RerankResult(BaseModel):
    ranked_ids: list[str]
    scores: dict[str, float]
    reasons: dict[str, str]

intent_agent = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[],
    output_schema=QueryIntent,                       # declared once (3.2)
    system_addendum=(
        "Classify the user's information need. Produce an optimized "
        "semantic search query. Keep natural-language context; do not "
        "strip the question down to bare keywords."
    ),
)

retriever = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[vector_search_tool(
        store,
        max_text_chars=0,          # full passages — the reranker needs evidence
        structured_output=True,    # JSON array of {id, text, vector_score, metadata}
        default_top_k=15, max_top_k=20,
    )],
    system_addendum=(
        "Use the QueryIntent from PRIOR SUB-TASK FINDINGS: call "
        "vector_search with its search_query and return the candidates."
    ),
)

reranker = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[],
    output_schema=RerankResult,
    system_addendum=(
        "Score each retrieved candidate against the user's ORIGINAL "
        "question (not the rewritten search query). Return ranked ids "
        "with a score and a one-line reason each."
    ),
)

sup = Supervisor(model=Claude(), agents={
    "intent":    ("Classifies the question, emits QueryIntent.", intent_agent),
    "retriever": ("Retrieves 15 candidates as structured JSON.", retriever),
    "reranker":  ("Judges relevance, emits RerankResult.", reranker),
})

result = sup.run("What is VelteHub's project-management module capable of?")

# Typed access after the run:
for sub in result.subtasks:
    if isinstance(sub.output, RerankResult):
        top = sub.output.ranked_ids[:3]     # deterministic Python from here on
```

Three 3.2 pieces make this work: constructor `output_schema` (schemas
declared once, coerced via native function calling AFTER the ReAct
loop), `SubtaskResult.output` (typed objects survive the Supervisor),
and structured findings threading (the retriever literally sees
`STRUCTURED OUTPUT (QueryIntent): {...}` in its context). Weighted
blending of vector vs. semantic scores belongs in YOUR Python, not in a
prompt — the LLM judges meaning, arithmetic stays deterministic.

**With 3.3, make the pipeline a DAG.** Register the specialists with
dependency hints and let the planner emit `depends_on` edges plus a
greeting short-circuit:

```python
from agentx_dev import Specialist

sup = Supervisor(model=Claude(), agents={
    "intent": Specialist(
        description="Classifies the question, emits QueryIntent.",
        runner=intent_agent,
        when_to_use="always first for knowledge questions",
    ),
    "retriever": Specialist(
        description="Retrieves 15 candidates as structured JSON.",
        runner=retriever,
        depends_on=["intent"],
    ),
    "reranker": Specialist(
        description="Judges relevance against the ORIGINAL question.",
        runner=reranker,
        depends_on=["retriever"],
    ),
})
```

The planner now emits step ids and edges; the retrieval step carries
`"skip_when": {"step": "intent", "field": "needs_rag", "is": false}`
so greetings never touch the vector store. Each step receives ONLY its
direct dependency's output, a failed retrieval skips the reranker
automatically (`skipped=True`), and under `AsyncSupervisor` any
independent steps in the same plan run concurrently.

---

## 25. Fan-out research → single synthesis, as a DAG *(3.3)*

The shape most multi-agent work actually has: several independent
investigations that must all land before one step can reason over the
whole picture. Pre-3.3 you chose between `sequential=True` (context
threading, no concurrency) and `sequential=False` (concurrency, no
context threading). 3.3 removes the choice — declare the edges and the
executor derives ordering, parallelism, and context routing from them.

```python
import asyncio
from pydantic import BaseModel, Field
from agentx_dev import AsyncSupervisor, Specialist, Claude


class MarketRead(BaseModel):
    signal: str = Field(description="What the data says")
    confidence: float = Field(description="0.0-1.0")


sup = AsyncSupervisor(
    model=Claude(),
    agents={
        # Three roots — no depends_on, so they start together.
        "filings":  Specialist(
            description="Reads SEC filings and 10-Ks.",
            runner=filings_agent,
            when_to_use="anything about reported financials",
        ),
        "news":     Specialist(
            description="Sweeps recent press and analyst notes.",
            runner=news_agent,
        ),
        "pricing":  Specialist(
            description="Pulls competitor pricing pages.",
            runner=pricing_agent,
        ),
        # One join — waits for all three, sees all three outputs.
        "analyst":  Specialist(
            description="Reconciles the three reads into a position.",
            runner=analyst_agent,
            depends_on=["filings", "news", "pricing"],
        ),
    },
    max_parallel=3,          # cap concurrency to stay under your TPM limit
)

result = asyncio.run(sup.run("Should we reprice the enterprise tier?"))

for sub in result.subtasks:
    if isinstance(sub.output, MarketRead):
        print(sub.step_id, sub.output.signal, sub.output.confidence)
```

What the scheduler does with that plan:

- **The three roots dispatch at once.** No wave barrier — the analyst
  starts the moment the *last* of its three dependencies finishes, not
  when some artificial round ends.
- **`analyst` sees exactly three findings**, labelled by step id. Not
  "everything that ran before it", which is what the old sequential
  mode threaded and what made context budgets shrink as plans grew.
- **`max_parallel=3`** is the throttle. Leave it `None` for unbounded;
  set it when you're fighting a tokens-per-minute ceiling. Note
  `sequential=True` is now just sugar for `max_parallel=1`.
- **If `pricing` fails** after `max_subtask_retries`, `analyst` is
  skipped transitively with `skipped=True` and an error naming the
  failed dependency. `filings` and `news` still complete, and synthesis
  runs over what survived — you get a partial answer instead of a
  burned run.

### Short-circuiting a branch

Give a step a `skip_when` and it never dispatches when the condition
holds — evaluated in Python against a dependency's typed output, so it
costs zero tokens:

```json
{"step": "triage", "field": "severity", "is": "low"}
```

Dotted paths work (`"field": "meta.tier"`). Evaluation is strictly
**fail-open**: a malformed condition, a missing field, or an untyped
dependency runs the step rather than silently dropping it. A skipped
step carries `skipped=True` with `error=None` — the reason lives in
`content`. That's the tell for distinguishing "condition matched" from
"dependency blew up".


---

## 26. Vision and document review agent *(3.4)*

An agent that looks at a scan and a PDF, compares them, and writes its
findings into its workspace. The text is the task; the files ride along
as media.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Media, Permissions

runner = AgentRunner(
    model=Claude(),                     # or GPT(model="gpt-4o")
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./inbox"]),
)

result = runner.invoke(
    "Compare the invoice to the receipt photo. List any line items that "
    "don't match, then save the list to mismatches.md.",
    media=[
        "./inbox/invoice.pdf",
        Media.image("./inbox/receipt.jpg", detail="high"),
    ],
)
print(result.content)
```

Why it's built this way:

- **The model sees files through `media=`, not `read_path`.**
  `read_path` is a text reader. Pointing the agent at a JPEG path in the
  prompt gets you "file is not utf-8 text" — attaching it gets you an
  answer.
- **Media paths are relative to your program**, not the workspace — hence
  `./inbox/receipt.jpg`, not `receipt.jpg`.
- **Writes land in the workspace.** `mismatches.md` goes to
  `./inbox/mismatches.md`. A leading slash would too: `/mismatches.md`
  means the workspace root.
- **Same code, either provider.** The one exception is audio: only GPT
  audio models accept it, and Claude raises `ValueError` before sending
  anything.

For a batch of documents, loop `runner.invoke(..., media=[path])` per
file, or use pattern #19 (the Batch API) for a 50% discount on
Claude-only workloads.

---

## 27. One agent across old and new models *(3.4)*

Configure models by what you *want*, and let each one settle on what it
supports. Useful when a config file picks the model, or when you run the
same evals across generations.

```python
from agentx_dev import AgentRunner, AgentType, GPT, Claude

MODELS = {
    "reasoning": GPT(model="gpt-5.4", reasoning_effort="none", max_tokens=4000),
    "legacy":    GPT(model="gpt-4o",  reasoning_effort="high", max_tokens=4000),
    "claude":    Claude(model="claude-sonnet-4-6", temperature=0.3, top_p=0.9),
    "claude-3":  Claude(model="claude-3-haiku-20240307", max_tokens=64000),
}

def run(task, which):
    runner = AgentRunner(model=MODELS[which], agent=AgentType.ReAct, tools=[])
    return runner.invoke(task).content
```

What each model does with the same intent:

| Model | Adjustment (logged once, then remembered) |
|---|---|
| `gpt-5.4` | `reasoning_effort` `'none'` → nearest supported value; `max_tokens` sent as `max_completion_tokens` |
| `gpt-4o` | `reasoning_effort` dropped — the model doesn't have it |
| `claude-sonnet-4-6` | if the model rejects `top_p` next to `temperature`, `top_p` is dropped |
| `claude-3-haiku` | `max_tokens` clamped to the model's output cap |

Things to know:

- **The first call to a model that rejects something costs one extra
  request.** After that the fix is remembered on that model object, so
  keep the object around (as the dict above does) rather than building a
  fresh `GPT(...)` for every call.
- **Want a hard failure instead?** In CI or config validation, pass
  `adapt_params=False` and a wrong setting raises the provider's `400`
  unchanged.
- **Only parameter errors are adjusted.** Context overflows, auth
  failures and bad requests of any other kind raise as usual.

---

## 28. A fixer that works through failures *(3.5)*

A task where the first attempts usually fail: run the tests, read the
failure, change the code, run again. Persistent mode lets the agent
keep going, and tells you honestly how it ended.

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Persistence

model = GPT(model="gpt-5.4").configure_limits(
    budget_usd=3.00, input_price_per_1k=0.0025, output_price_per_1k=0.01)

fixer = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./project"]),
    system_addendum="Run the tests with run_python (subprocess + pytest). "
                    "Do not say you are done until the tests pass.",
    persistence=Persistence(max_minutes=30),
)

result = fixer.invoke("Make the tests in ./project pass")
if result.outcome == "done":
    print(result.content)
else:
    print(f"Stopped ({result.outcome}):")
    print(result.content)              # what was done, what failed, what was next
    print(result.progress["failed"])   # the failed attempts, as data
```

Things to know:

- **Tell it how to check its own work.** Persistence keeps the agent
  going; the instruction to run the tests is what makes "done" mean
  something.
- **A stuck run is still useful.** The report lists what was tried, so
  you can fix the task or the tools and run it again.
- **Watch it:** `for event in fixer.stream(task)` includes `reflect`
  events when the agent changes approach.

---

## 29. A supervisor that hires its own helpers *(3.6)*

The task decides which specialists it needs, and you decide the most any of them may do.

```python
from agentx_dev import Persistence, SpawnConfig, Supervisor

supervisor = Supervisor(
    model=model,
    agents={"writer": ("Drafts the final brief", writer)},      # the one specialist you know you need
    persistence=Persistence(max_minutes=45),
    spawn_config=SpawnConfig(
        enabled=True,
        capabilities={"web", "files_read"},     # helpers may search the web and read files, nothing else
        allowed_paths=["./workspace"],
        max_spawns=6,
    ),
)

result = supervisor.run(
    "Research our three closest competitors' pricing, then write a one-page brief."
)
print(result.content)
for sub in result.spawned:
    print(sub["name"], sub["origin"], sub["outcome"])
```

What happens: the planner defines a `pricing_researcher` helper with its own instructions and the `web` tool, runs it for each competitor, and hands the findings to `writer`. If a research step gets stuck, the recovery plan can define a differently-instructed replacement.

Things to know:

- **Say what to return.** The planner writes the helper's instructions; the framework adds "return the real data, don't invent it". Your task text should say what the brief must contain.
- **The ceiling is yours.** Without `code`, `files` or `delete` in `capabilities`, no plan can give a helper those, however the task is worded.
- **Helpers are for one run.** `supervisor.agents` is unchanged afterwards; `result.spawned` is the record.
- **A specialist can do this itself** with the `delegate` tool, to keep its own context small. See [Sub-agents](../guides/sub-agents.md).
- **Your own specialists** are described to the planner by their name and description; see pattern 31 for `Specialist`, `when_to_use` and how to write them.

---

## 30. One agent that hands side jobs to helpers *(3.6)*

You don't need a Supervisor to use sub-agents. Give a single runner `delegation=` and it gets a `delegate` tool: it can hand a self-contained piece of work to a fresh agent, which works in a clean context and sends back a short summary. The runner's own context stays small, however big the side job is.

```python
from agentx_dev import AgentRunner, AgentType, Persistence, SpawnConfig

researcher = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    tools=[],                                              # no tools of its own: it delegates the searching
    system_addendum=(
        "You write market briefs. For each competitor, call delegate(task=..., "
        "instructions='Return their plans, prices and source URLs as a table.', tools=['web']). "
        "Put everything the helper needs in `task`: it can't see this conversation. "
        "Then combine the helpers' answers into one brief."
    ),
    persistence=Persistence(max_minutes=30),               # optional: helpers inherit it
    delegation=SpawnConfig(
        enabled=True,
        capabilities={"web"},          # helpers may search and fetch, nothing else
        max_spawns=5,                  # delegations per run
    ),
)

result = researcher.invoke("Brief me on the pricing of Acme, Globex and Initech.")
print(result.content)

for helper in researcher.spawned:                          # this run's helpers
    print(helper["name"], helper["tools"], helper["outcome"], helper["chars"])
```

Each helper is named `delegate_1`, `delegate_2`, ... and is gone when the run ends. `researcher.spawned` is reset at the start of every run.

The async runner works the same way. Pass `bind_tools_natively=True` and a turn that makes several `delegate` calls runs them at the same time:

```python
from agentx_dev import AsyncAgentRunner, AgentType, SpawnConfig

researcher = AsyncAgentRunner(
    model=model, agent=AgentType.ReAct, tools=[], bind_tools_natively=True,
    delegation=SpawnConfig(enabled=True, capabilities={"web"}),
)
result = await researcher.ainvoke("Brief me on Acme, Globex and Initech, one helper each.")
```

Things to know:

- **The helper sees only `task`.** A vague `task` gets a vague answer, so tell the agent (in `system_addendum` or the prompt) to include every fact the helper needs.
- **Failures come back as tool errors, not crashes.** A helper that gives up or crashes returns `[delegate failed: stuck]` (or `error`, `out_of_time`, ...) as a tool error. The agent can retry with different instructions or do the work itself. A refusal (`delegation refused: spawn limit reached; do this yourself`) is plain text.
- **The ceiling is yours.** `capabilities=` / `tools=` decide what any helper can use. Whatever the model asks for outside it is dropped, and the result says what was dropped.
- **Helpers can't delegate further** at the default `max_depth=1`.
- **One run at a time.** A runner built with `delegation=` keeps its run's state on the instance, so don't call it concurrently with itself; use one runner per concurrent run.
- **It doesn't use the runner's own `permissions=`.** `delegate` grants tools from the `SpawnConfig` ceiling only.
- For a Supervisor that spawns helpers across a whole plan, see pattern 29 above and the [Sub-agents guide](../guides/sub-agents.md).

---

## 31. Tell the planner what each specialist is for *(3.3, 3.6)*

A Supervisor's planner never sees your agents' code or their `system_addendum`. All it sees is a short catalog: each agent's **name**, its **description**, and (if you use `Specialist`) a few extra lines. That catalog is the only thing it uses to decide who gets which step, and whether it needs to create a new helper (3.6). So there are two different texts, and they do different jobs:

| Text | Who reads it | What it is for |
|---|---|---|
| `system_addendum` on the `AgentRunner` | the agent itself | how to behave: its role, rules, what to return |
| the description in `agents={...}` (or `Specialist(description=, when_to_use=)`) | the Supervisor's planner | when to pick this agent, what it returns, what it cannot do |

A plain `("description", runner)` tuple works, but `Specialist` lets you say more:

```python
from pydantic import BaseModel, Field
from agentx_dev import (
    AgentRunner, AgentType, Specialist, StructuredTool, Supervisor, SpawnConfig,
)

class Findings(BaseModel):
    summary: str
    sources: list[str]

# A human-in-the-loop tool. input() works in a terminal and in Jupyter.
class AskArgs(BaseModel):
    question: str = Field(..., description="One short, self-contained question for the operator.")

def ask(question: str) -> str:
    reply = input(f"\n[agent asks] {question}\n> ").strip()
    return f"[operator] {reply}" if reply else "(operator gave no answer)"

ask_human = StructuredTool(func=ask, args_schema=AskArgs, name="ask_human",
                           description="Ask the human operator one question and return the typed reply.")

human = AgentRunner(
    model=model, agent=AgentType.ReAct, tools=[ask_human], verbose=False,
    system_addendum=(                      # written FOR THE AGENT
        "You are the liaison to the human operator. Call ask_human exactly once with one "
        "short, self-contained question, then report the question and the operator's reply "
        "verbatim. Never guess or invent the operator's answer."
    ),
)
researcher = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                         output_schema=Findings)          # (give it web tools in real use)
writer = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)

supervisor = Supervisor(
    model=model,
    agents={
        "human": Specialist(                  # written FOR THE PLANNER
            description="Asks the human operator ONE clarifying question and returns their "
                        "typed answer verbatim. The only agent that can get information the "
                        "task leaves out. Cannot search, read files or do other work.",
            runner=human,
            when_to_use="Make this the FIRST step whenever the task refers to something it "
                        "does not name (for example 'our competitors', 'the project', 'that "
                        "file') or to any detail you would otherwise have to guess. Put the "
                        "exact question in the query. Every step that needs the answer must "
                        "list this step in depends_on. Do not research or guess before it "
                        "has answered.",
        ),
        "researcher": Specialist(
            description="Searches the web and returns findings with sources.",
            runner=researcher,                # its output_schema is shown to the planner
        ),
        "writer": Specialist(
            description="Writes the final brief from findings it is given.",
            runner=writer,
            depends_on=["researcher"],        # a hint: usually runs after the researcher
        ),
    },
    spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
)

print(supervisor._build_agent_catalog())      # exactly what the planner reads (a debugging peek)
```

That last line prints:

```
- human: Asks the human operator ONE clarifying question and returns their typed answer verbatim. The only agent that can get information the task leaves out. Cannot search, read files or do other work.
    use when: Make this the FIRST step whenever the task refers to something it does not name (for example 'our competitors', 'the project', 'that file') or to any detail you would otherwise have to guess. Put the exact question in the query. Every step that needs the answer must list this step in depends_on. Do not research or guess before it has answered.
- researcher: Searches the web and returns findings with sources.
    returns: Findings(summary, sources)
- writer: Writes the final brief from findings it is given.
    typically after: researcher
```

What each `Specialist` field does:

- **`description`**: what it does and returns, in one or two sentences. Say what it **cannot** do too ("cannot browse the web"): that is how the planner knows to create a web helper (3.6) instead of sending web work to it.
- **`when_to_use`**: routing advice, shown as `use when:`. Good for rare or expensive agents like the human one.
- **`depends_on`**: names this agent *typically* follows. A hint only, never a rule.
- **`output_schema`**: taken from the runner's `output_schema` unless you set it. The planner sees the field names, so it can write `skip_when` conditions against real fields.

### Making the planner actually use an agent like `human`

Choosing steps is a single planning call that happens *before* anything runs, and the planner is told to prefer the shortest plan. So an agent that only makes sense in some situations gets skipped unless the catalog makes its trigger concrete:

- **Write `when_to_use` as an instruction with a trigger**, not a hedge. "Only when genuinely ambiguous" never fires, because the planner doesn't see the task as ambiguous; "FIRST step whenever the task mentions something unnamed ('our competitors', 'the project')" does.
- **Tell it how the answer flows:** "every step that needs the answer must list this step in `depends_on`". The answer is then handed to those steps as prior findings.
- **Say it in the task too** when you can: "If anything is missing, ask the human first." That is the most reliable lever.
- **Without `ask_user`, helpers a plan creates can't ask the operator.** If a new helper discovers mid-step that something is missing, it can only report that in its answer, which becomes part of the final answer (that is what happened in a run that created a web helper and ended by asking you for the competitor names). Getting the question asked is then the planner's job, up front, through an agent like `human`. With `ask_user` set on the Supervisor, helpers get an `ask_user` tool and can ask mid-run; see pattern 32.

Pattern 31 still works when you want a human step the planner must plan. For the built-in way to ask, where the Supervisor itself asks for what the task leaves out, see pattern 32.

Things to know:

- **Keep names short** (`"human"`, not `"Human in the Loop"`): the planner types them into plan steps.
- **Describe the agent, not its prompt.** If you paste the `system_addendum` into the description you are spending the planner's attention on rules it can't use.
- **The tuple and `Specialist` forms mix freely** in one `agents` dict.
- **Check what the planner reads** with `supervisor._build_agent_catalog()` (a private method, handy for debugging). If a description doesn't tell *you* when to use the agent, it won't tell the planner either.

---

## 32. Let the Supervisor ask you for missing facts (`ask_user`) *(3.6)*

"Compare the pricing pages of our three competitors" never names the competitors. The planner is pushed toward the shortest plan, so it plans anyway, or spawns a web helper that guesses. A human-in-the-loop tool built on `CONIN$` or `/dev/tty` can also hang forever in Jupyter: that is the console of the kernel process, not the notebook, so the prompt appears where nobody is looking. With `ask_user` set, the planner can ask before it plans and every agent can ask mid-run, on a channel that fits where your code runs.

In a notebook or a terminal, `ask_user=True` is enough:

```python
from agentx_dev import Supervisor, SpawnConfig

supervisor = Supervisor(
    model=model,
    agents={"writer": ("Writes the final brief from findings it is given", writer)},
    spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
    ask_user=True,        # notebook: the input box under the cell; terminal: input()
)

result = supervisor.run("Compare the pricing pages of our three competitors")
print(result.content)
for q in result.asked:    # source, question, answered, reason, deduped (no answer text)
    print(q["source"], q["question"], q["answered"], q["reason"])
```

What happens: the planner replies with a question ("Which three competitors?") instead of a plan, you type `Notion, Obsidian, Coda`, and planning runs again with your answer added to the task. A helper that hits another gap later can call its own `ask_user` tool.

A chatbot or backend has no terminal, so route the question through your own channel with a function. Returning `None` means nobody answered:

```python
def ask_via_chat(question: str) -> str | None:
    session.send(question)                        # your UI, websocket or queue
    return session.wait_for_reply(timeout=120)    # None = no answer

supervisor = Supervisor(model=model, agents=agents, ask_user=ask_via_chat,
                        max_questions=3, ask_timeout=120)
```

On an `AsyncSupervisor` the function may be `async def` (with async specialists; see below), or a plain function, which runs in a worker thread.

Things to know:

- **Budget and timeout.** `max_questions` (default 3) is one budget for the whole run, planner and agents together, and each agent may ask only once (the planner may ask several questions in its one round), so one helper cannot use the whole budget clarifying. `ask_timeout` bounds the wait for one answer; it is not enforced on a notebook's input box, where the Interrupt button stops the run.
- **No answer is not a failure.** Headless, a raised exception, a timeout, an empty reply or a spent budget all tell the agent to proceed on a stated assumption. `ask_user=True` on a server therefore never asks; pass a function there.
- **When the planner still does not ask,** say it in the task: "If anything is missing, ask the operator first." Agents can still ask mid-run through the `ask_user` tool.
- **`async def` needs async specialists.** A sync specialist under `AsyncSupervisor` asks through the synchronous path, which cannot await an async function; with sync specialists pass a plain function.
- See [Asking the operator](../guides/sub-agents.md#asking-the-operator) for the events, the `result.asked` fields and the limits.
