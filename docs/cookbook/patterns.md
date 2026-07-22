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
        async for event in runner.astream(req["query"], stream_tokens=True):
            if event["type"] == "completion": continue
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

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

## 16. Cache invalidation on tool changes

Tools whose inputs shouldn't cache (e.g. `current_time`) should NOT go
through `runner.registry.cache`. Attach cache to specific tools only:

```python
runner.registry.configure_cache(get_global_cache())

# Then override for tools you DON'T want cached:
class NoCacheTool(StandardTool):
    def dispatch(self, args):
        return self.func(args)

runner.registry.tools_by_name["current_time"] = NoCacheTool(func=..., name="current_time", ...)
```

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
