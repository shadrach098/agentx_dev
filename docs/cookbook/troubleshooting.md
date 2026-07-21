# Troubleshooting

Common errors and their fixes.

## Import errors

**`ImportError: No module named 'anthropic'`**
Install the extra: `pip install agentx-dev[anthropic]`.

**`ImportError: No module named 'mcp'`**
Install the extra: `pip install agentx-dev[mcp]`.

**`ImportError: No module named 'agentx_dev.MCP'`**
Same fix. The framework imports MCP lazily; only fails on use.

## Model construction

**`OpenAI 400: Invalid value for 'parallel_tool_calls': 'parallel_tool_calls' is only allowed when 'tools' are specified.`**
Don't set `parallel_tool_calls=True` on the `GPT` model. It defaults on
for tool-using calls; setting at the model level leaks into tool-less
calls. Remove the arg.

**`AuthenticationError`**
Check your `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env var.

**`ValueError: The 'model' object must inherit from BaseChatModel`**
You passed a raw provider client. Wrap it in a `BaseChatModel`
subclass.

## Runner errors

**`TypeError: AgentRunner received both 'Agent' and 'agent'`**
Pick one — new code should use lowercase `agent=`.

**`ValueError: use_function_calling and bind_tools_natively are mutually exclusive`**
Pick one. Function calling routes through the parser; native binding
skips it.

**`ValueError: The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}`**
Your custom template is missing one of those placeholders. All three
are required.

## Tool errors

**Model calls the wrong tool**
- Improve descriptions.
- Say "Prefer over X when ..." and "Do NOT use for ..." explicitly.
- Reduce tool count; the more tools, the noisier the choice.

**`ToolError: refused: 'foo' has been called N times in a row`**
Dup-guard tripped. Fix the model's prompt so it uses prior results
instead of re-issuing. Change `max_iterations` if the model needs to
try more DIFFERENT calls.

**`TimeoutError: tool exceeded Ns timeout`**
Set a larger `tool.timeout_sec` on the specific tool, or make the tool
async and use `AsyncAgentRunner` so timeout cancels cleanly.

**`ToolError: circuit breaker open for 'X': tripped after N consecutive failures`**
The tool has been failing. Wait `recovery_timeout_sec` and it enters
half-open state (one probe call allowed). If the underlying issue is
fixed, call `runner.registry.get_breaker('X').reset()`.

## Permission / sandbox errors

**Tool "read_path" not available**
The `read_files` capability isn't granted in your `Permissions`. Set
it to `True`.

**`PermissionError: path outside sandbox`**
The path resolves outside `allowed_paths`. Either widen the sandbox or
use a path inside it.

**`.agentx/permissions.json` refuses to load**
Check file mode (should be 0o600, only readable by you). Fix:
`chmod 0600 .agentx/permissions.json`.

## Handoff errors

**`(handoff to unknown agent 'X' — no route)`**
Your agent tried to hand off to a name not in the coordinator's dict.
Add the missing agent or fix the handoff tool's `target=`.

**`(handoff loop exceeded max_hops=N)`**
Two agents ping-pong. Increase `max_hops`, or reshape the specialists
to break the cycle.

**OpenAI 400: "messages with role 'function' must have a 'name'"**
Fixed in 3.1's `_sanitize_history_for_next_agent`. If you see it, you
may be on an older build; upgrade.

## Cost / rate limit

**`CostBudgetExceeded: spent $X, limit $Y`**
Increase `budget_usd` or reduce the task. The agent halted the moment
cumulative spend crossed the cap.

**`RetryBudgetExceeded`**
The upstream keeps returning transient errors. Investigate rate
limits, quotas, or transient outages upstream. Reset with:

```python
llm._retries_used = 0
```

## Cache issues

**Same query returns stale results**
Cache TTL hasn't expired. Clear it: `runner.registry.cache.clear()` or
skip cache for this tool.

**Cache never hits**
Args differ across calls even for what looks like the "same" query.
The cache key is `(name, sorted-json-args)` — check for whitespace,
casing, ordering differences.

## Session issues

**`RuntimeError: Session has no runner attached`**
Call `session.attach(runner)` before any invoke.

**`ValueError: Session file X has schema version 2, but this build only knows up to 1`**
Upgrade `agentx-dev`, or downgrade the session file.

**`ValueError: Session file X is not valid JSON`**
File got corrupted (crashed write, wrong encoding). Check the `.tmp`
sibling — the framework writes atomically so a partial write should
leave `.tmp` behind, not corrupt the main file.

## Memory / RAG issues

**`ValueError: Embedding dim mismatch: store has X, new vectors have Y`**
You switched embedding models mid-collection. Reindex from scratch
with the new backend, or use a separate store.

**`SemanticMemory` returns nothing**
- Did you call `mem.set_query(...)` before `get_messages`?
- Is `min_score` too high for your embedding backend? Try `0.0` first
  to see what's actually being scored.
- Is the store empty? Check `len(mem._store)`.

**`vector_search` returns irrelevant results**
- Try `OpenAIEmbeddings` instead of `HashEmbeddings` — much better
  recall.
- Chunk size too large or too small. 500-1500 char chunks with 100-300
  char overlap is a reasonable starting point.
- Add source-file metadata so the LLM can weigh authority.

## Streaming issues

**Nothing prints**
`print(..., flush=True)` or the console buffers.

**Streaming stops mid-response**
Provider timeout or connection drop. Retry or lower the effective
response length.

**`text_delta` events never fire**
You're in `use_function_calling=True` mode. Text-delta events only
fire in text mode.

**Supervisor / Handoff `stream()` events don't arrive to my UI *(3.1)***
The `completion` (Supervisor) and `result` (Handoff) events carry
non-JSON-serializable dataclasses (`SupervisorResult`,
`HandoffResult`). Serialize their `.completion.content` and
`.hops` fields explicitly before yielding to SSE / websockets.
See patterns §21.

## Prompt caching *(3.1)*

**`cache_hit_ratio` stays at 0.0**
Prompt caching is Anthropic-only. Set `Claude(enable_prompt_cache=True)`.
If it's on and still 0.0: (a) first call always misses (creates the
cache); (b) 5-minute TTL expired between calls; (c) system prompt or
tool list changed between calls — any drift invalidates the cache.

**Anthropic 400 on `cache_control`**
Your anthropic SDK is too old. Bump to `>=0.36`:
`pip install -U 'anthropic>=0.36'`.

## Batch API *(3.1)*

**`RuntimeError: This anthropic SDK version does not expose the Batch API`**
Same as above — upgrade `anthropic>=0.36`.

**`TimeoutError: batch did not finish within Ns`**
Bump `max_wait_sec` (Anthropic's TTL is 24h, so up to 86400). For
huge batches, poll less often (`poll_interval_sec=60`) to reduce
request overhead.

**Some results come back as `{"error": ..., "type": "errored"}`**
Per-request failure — the batch itself succeeded. Loop over the
results and dispatch dicts vs. strings:

```python
for i, r in enumerate(results):
    if isinstance(r, dict):
        print(f"[{i}] {r['type']}: {r['error']}")
    else:
        process(r)
```

## Compiled optimizer *(3.1)*

**`Compiled.compile()` never improves over baseline**
- Trainset too small (< 5 cases) — teacher can't see enough failure
  signal. Add more cases.
- Assertions too permissive — everything passes baseline. Tighten
  assertions so the baseline actually fails some cases.
- Teacher model is too weak — pass a stronger `teacher_model=` (Opus
  or Sonnet, not Haiku) via the constructor.

**Best addendum overfits — helps train, hurts prod**
Split your suite into `trainset` (fed to `Compiled`) and `holdset`
(evaluated with `EvalRunner` after compile). Only ship the tuned
addendum if `holdset` score improves too.

## Vector store adapter errors *(3.1)*

**`ImportError: ChromaVectorStore requires the chromadb package`**
Install the extra: `pip install agentx-dev[chroma]` (or `[qdrant]` /
`[pgvector]`).

**`ValueError: Embedding dim mismatch` after switching adapters**
The adapter shape is identical but the *backend* stores vectors on
disk / server. Reindex from scratch when moving between adapters or
between embedding models.

**Qdrant: "collection X vector dim Y != expected Z"**
The collection was created with a different embedding dim. Delete
the collection first (`client.delete_collection(name)`) and let the
adapter recreate it.

**Postgres: `CREATE EXTENSION vector` fails**
The `pgvector` extension isn't installed on the DB. On managed
Postgres (RDS / Cloud SQL) you must enable it via console first;
on self-hosted, `apt install postgresql-16-pgvector` (or equivalent).

## Debug tooling

**Enable verbose logging:**

```python
import logging
logging.getLogger("agentx_dev").setLevel(logging.DEBUG)
```

**Enable observability:**

```python
from agentx_dev import config, observability, ConsoleHook
config.observability_enabled = True
observability.add_hook(ConsoleHook(verbose=True))
```

**Inspect the working history:**

```python
result = runner.invoke("...")
for m in result.history:
    print(f"[{m['role']}] {str(m.get('content'))[:120]}")
```

**Inspect every tool call:**

```python
for tc in result.tool_calls:
    print(f"{tc.name}({tc.args})")
    print(f"  -> {str(tc.result)[:120]}")
```

**Trace the plan (Supervisor):**

```python
supervisor = Supervisor(model=llm, agents=..., verbose=True)
```

**Trace hops (Handoffs):**

```python
result = coord.run("...")
print(result.hops)
```
