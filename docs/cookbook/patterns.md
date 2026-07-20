# Patterns cookbook

Reusable shapes for common problems.

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
