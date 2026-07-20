# Guide: author your own tools

Two flavors: `StandardTool` (one string arg) and `StructuredTool`
(typed multi-arg via Pydantic). Async variants exist for I/O-bound
tools.

## Boilerplate

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class SearchArgs(BaseModel):
    query: str = Field(..., description="The search query text.")
    num_results: int = Field(5, description="How many results to return, max 20.")
    site: str = Field("", description="Restrict to this domain, e.g. 'github.com'.")

def search(query: str, num_results: int = 5, site: str = "") -> str:
    """Return top matches as a numbered list."""
    # your search implementation
    return f"1. Result 1\n2. Result 2\n..."

search_tool = StructuredTool(
    func=search,
    args_schema=SearchArgs,
    name="search",
    description=(
        "Web search. Returns numbered results with titles + URLs. "
        "Prefer over web_fetch when the URL isn't known. Do NOT use "
        "for the current time or math questions."
    ),
)
```

## Descriptions are the LLM's UI

The LLM picks tools based on descriptions. Be specific:

- **What** does the tool do?
- **When** should the model use it vs. another tool?
- **When NOT** to use it?
- **Input** shape (types, examples).
- **Output** shape (what does the model get back?).

Bad:
```
description="Weather tool."
```

Good:
```
description=(
    "Get the current weather for a city. "
    "Input: {city: string}. Output: '<temperature>C, <conditions>' string. "
    "Prefer over web_search for weather questions -- faster and structured."
)
```

## When to use StandardTool vs. StructuredTool

- **StandardTool** — one string in, one string out. Simplest LLM interface.
  Great for wrapping single-purpose CLIs (e.g. `translate`, `sentiment`).
- **StructuredTool** — multiple typed fields. Pydantic validates before
  your function runs. Use when the model needs to fill more than one
  parameter.

## Async tools

For I/O-bound tools (HTTP, DB, disk async):

```python
import aiohttp
from agentx_dev import AsyncStandardTool

async def http_get(url: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.text()

http_tool = AsyncStandardTool(
    func=http_get, name="http_get",
    description="HTTP GET a URL and return the response body as text.",
)
```

Async structured:

```python
from pydantic import BaseModel
from agentx_dev import AsyncStructuredTool

class DBQueryArgs(BaseModel):
    sql: str
    limit: int = 100

async def run_query(sql: str, limit: int = 100) -> str:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql + f" LIMIT {limit}")
        return "\n".join(str(r) for r in rows)

db_tool = AsyncStructuredTool(
    func=run_query, args_schema=DBQueryArgs,
    name="db_query", description="Run a read-only SQL query.",
)
```

Async tools ONLY work with `AsyncAgentRunner`. Passing them to
`AgentRunner` (sync) works too — the framework can host them — but
they're dispatched synchronously via the async event loop.

## Per-tool controls

Attach attrs directly to the tool instance:

```python
search_tool.timeout_sec = 15                        # kill after 15s
search_tool.circuit_breaker = CircuitBreakerConfig( # tool-specific override
    failure_threshold=3, recovery_timeout_sec=30,
)
```

Or register with the framework's registry-wide defaults — see
[Production controls](../advanced/production-controls.md).

## Error handling

Return a string on success. On failure:

- Raise an exception — the framework wraps it as `ToolError`.
- Or return a `ToolError` directly for a controlled error message:

```python
from agentx_dev.Agents.Agent import ToolError

def search(query: str) -> str | ToolError:
    if not query.strip():
        return ToolError("Empty query", tool="search")
    try:
        return _search_impl(query)
    except RateLimitError as e:
        return ToolError(f"Rate limited: {e}", tool="search", cause=e)
```

The agent sees the error text and can react — often retrying with
different arguments or emitting Final_Answer.

## Caching results

Wrap with `cached_tool` for automatic caching:

```python
from agentx_dev import cached_tool, LRUCache

cache = LRUCache(max_size=1000, ttl=3600)   # 1h TTL

cached_search = cached_tool(search_tool, cache=cache)
```

Or attach a global cache at runner level:

```python
from agentx_dev import get_global_cache
runner.registry.configure_cache(get_global_cache(), cache_ttl=3600)
```

## Web tools

Two ship in-tree; they're factories that build a StructuredTool:

```python
from agentx_dev import web_search_tool, web_fetch_tool

runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[
        web_search_tool(),                         # DuckDuckGo + Wikipedia fallback
        web_fetch_tool(cache_dir="./workspace"),   # optional disk cache
    ],
)
```

**`web_search_tool()`** — no API keys, no extra deps. DuckDuckGo with a
Wikipedia fallback. Returns titles + URLs + snippets.

**`web_fetch_tool(cache_dir=None)`** — GET a URL. When `cache_dir` is set,
the full response body lands on disk and the reply includes a
`open(cached_path).read()` snippet the model can drop straight into
`run_python`. Saves LLM tokens on huge pages.

**SSRF guard** (3.0.6) — `web_fetch` rejects non-public destinations
(loopback, RFC1918, 169.254.169.254 cloud metadata, link-local,
multicast) and re-validates every redirect hop.

## Testing tools

Tools are ordinary callables — test them directly:

```python
def test_search():
    result = search_tool.func(query="python", num_results=3)
    assert "python" in result.lower()
```

Or test the whole runner:

```python
def test_runner_uses_search():
    runner = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[search_tool])
    result = runner.invoke("Search for python tutorials")
    assert any(tc.name == "search" for tc in result.tool_calls)
```

For full regression testing, use the [evals harness](../advanced/evals.md).

## Common patterns

### Wrap a class-method as a tool

```python
class SlackClient:
    def __init__(self, token): self.token = token
    def post(self, channel: str, text: str) -> str: ...

client = SlackClient(token=os.environ["SLACK_TOKEN"])

slack_tool = StructuredTool(
    func=client.post,   # bound method works fine
    args_schema=SlackPostArgs,
    name="slack_post",
    description="Post to a Slack channel.",
)
```

### Factory tools (one function that builds many)

```python
def make_db_tool(pool, name: str, description: str) -> StructuredTool:
    async def run(sql: str) -> str:
        async with pool.acquire() as c:
            return str(await c.fetch(sql))
    return AsyncStructuredTool(
        func=run,
        args_schema=SQLArgs,
        name=name,
        description=description,
    )

tools = [
    make_db_tool(users_pool, "query_users", "Query the users database."),
    make_db_tool(orders_pool, "query_orders", "Query the orders database."),
]
```
