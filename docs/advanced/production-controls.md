# Production controls

Levers for running agents in production without blowing your budget or
wedging on a bad tool.

## Cost budgets

Halt the agent when spend crosses a cap:

```python
from agentx_dev import Claude

llm = Claude(model="claude-sonnet-4-6").configure_limits(
    budget_usd=5.0,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,
)
```

After every response with usage data, cumulative spend is recomputed. A
crossing raises `CostBudgetExceeded(spent_usd=..., limit_usd=...)`
immediately — no more API calls, no partial answer, caller catches the
exception.

You provide the prices — the framework doesn't bake in a pricing table
(changes too often). Pull them from the provider's pricing page.

## Rate limits

Token-bucket, request-level:

```python
llm.configure_limits(
    rate_limit_per_sec=5,     # 5 requests/second sustained
    rate_limit_burst=10,      # allow bursts up to 10 accumulated tokens
)
```

Every attempt (including retries) counts against the bucket. Runs
block until a token is available.

## Retry budget

Cap total retries over the model's lifetime:

```python
llm.configure_limits(retry_budget=10)
```

When exhausted, `RetryBudgetExceeded` is raised on the next retry
attempt. Prevents a misbehaving upstream (or a flaky endpoint) from
burning through your API quota one retry at a time.

## All three at once

```python
llm = Claude(model="claude-sonnet-4-6").configure_limits(
    budget_usd=5.0,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,
    rate_limit_per_sec=5,
    rate_limit_burst=10,
    retry_budget=10,
)
```

Everything's opt-in; unset params leave that lever off.

## Retry policy

Automatic exponential backoff on transient failures:

- **408 Request Timeout** — retry.
- **429 Too Many Requests** — retry (whole point of exponential backoff).
- **400 / 401 / 403 / 404 / 422** — DO NOT retry. Not transient;
  wasting retries just burns budget.
- Everything else — retry.

Configurable via `max_retries` on the model constructor. Backoff is
`base_delay * (2 ** attempt)`.

## Tool timeouts

Per registry:

```python
from agentx_dev import ToolRegistry

runner.registry = ToolRegistry(
    runner.registry.tools,
    default_timeout_sec=30,   # cap wall-clock per tool dispatch
)
```

Per tool (overrides registry default):

```python
tool.timeout_sec = 60
```

Sync timeouts run the dispatch on a `ThreadPoolExecutor`. On timeout,
the caller gets a `ToolError` back, but Python can't kill the
underlying thread — a misbehaving sync tool leaks one thread. Async
timeouts use `asyncio.wait_for` which cancels cleanly. Prefer async for
anything that might hang.

## Circuit breakers

Trip after N consecutive failures; short-circuit subsequent calls
until recovery:

```python
from agentx_dev import ToolRegistry, CircuitBreakerConfig

runner.registry = ToolRegistry(
    runner.registry.tools,
    circuit_breaker_config=CircuitBreakerConfig(
        failure_threshold=5,      # trip after 5 in a row
        recovery_timeout_sec=60,  # try again after 1 minute
    ),
)
```

Per-tool override:

```python
tool.circuit_breaker = CircuitBreakerConfig(failure_threshold=3)
```

State machine:

```
closed --(N failures)--> open --(recovery_timeout)--> half_open
half_open --(success)--> closed
half_open --(failure)--> open (fresh timeout)
```

Open-state calls return `ToolError("circuit breaker open ...")`
immediately without touching the underlying tool.

Inspect a breaker for ops dashboards:

```python
breaker = runner.registry.get_breaker("http_get")
print(breaker.state, breaker._consecutive_failures)
```

## Tool result cache

Memoize expensive tool results:

```python
from agentx_dev import get_global_cache, config

runner.registry.configure_cache(get_global_cache(), cache_ttl=config.cache_ttl)
```

Or attach a specific cache:

```python
from agentx_dev import LRUCache

cache = LRUCache(max_size=1000, ttl=3600)
runner.registry.configure_cache(cache, cache_ttl=3600)
```

Ships:

- `InMemoryCache` — dict-backed, no eviction.
- `LRUCache(max_size, ttl)` — eviction + TTL.
- `FileCache(directory, ttl)` — disk-backed, survives restarts.

Cache keys are `generate_cache_key(tool_name, args)` — deterministic
across runs.

**When NOT to cache** — tools with side effects (`send_email`,
`write_file`, `delete_path`), tools whose results change with time
(`current_time`, `stock_price`).

## Duplicate-call guard

Enabled by default in every `ToolRegistry`. Refuses runaway models:

```
1st, 2nd identical call: normal
3rd, 4th: response prepended with "[framework] WARNING: repeat call #N"
5th+: ToolError("refused. Framework prevents spinning on same call")
```

Any DIFFERENT call resets both counters.

## Loop-level force-stop

At the runner level: 3 consecutive identical `(action, args)` pairs
and the loop force-terminates BEFORE dispatching the third call.
Synthesizes a final answer from the last successful tool result.

Both guards fire independently — belt + suspenders against sticky
model behavior.

## Combining everything

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    ToolRegistry, CircuitBreakerConfig,
    LRUCache,
)

llm = (
    Claude(model="claude-sonnet-4-6", enable_prompt_cache=True)
    .configure_limits(
        budget_usd=5.0,
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
        rate_limit_per_sec=5,
        retry_budget=10,
    )
)

runner = AgentRunner(
    model=llm,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    max_iterations=8,
)

runner.registry = ToolRegistry(
    runner.registry.tools,
    default_timeout_sec=30,
    circuit_breaker_config=CircuitBreakerConfig(
        failure_threshold=5, recovery_timeout_sec=60,
    ),
)
runner.registry.configure_cache(LRUCache(max_size=1000, ttl=3600))
```

Every dispatched call now has: sandbox → dup-guard → cache → circuit
breaker → timeout → observability. Every LLM call has: rate limit →
retry → cost budget. Prompt caching cuts input costs; result cache
cuts repeat-tool costs; circuit breakers isolate flaky tools; budgets
cap the total.

## Observability + these controls

Every guard fires a matching observability event:

- `CIRCUIT_OPEN` — breaker trips.
- `CACHE_HIT` — cache serves the result.
- Duplicate-guard writes to the observability event `data.repeat_count`.
- Rate-limit waits go into `LLM_CALL_START.data.rate_limit_wait_sec`.

Wire an `OTelHook` or `MetricsHook` and you get Prometheus-shaped
dashboards for free. See [Observability](../concepts/observability.md).
