# Anthropic Batch API

Anthropic's Batch API charges **50%** of standard pricing. Best for
embarrassingly-parallel workloads (evals, data labeling, bulk
extraction) where a few minutes of latency is fine.

`Claude.batch()` submits the requests, polls until done, and returns
results in the same order.

## Usage

```python
from agentx_dev import Claude

llm = Claude(model="claude-sonnet-4-6")

prompts = [
    "Summarize this in one sentence: <text 1>",
    "Summarize this in one sentence: <text 2>",
    "Summarize this in one sentence: <text 3>",
    # ... hundreds or thousands
]

results = llm.batch(prompts)          # blocks until all done
for r in results:
    print(r)
```

Each item in `results` is either:
- **a string** -- the assistant's text response, on success.
- **a dict `{"error": str, "type": str}`** -- on per-request failure
  (validation, over-limit, canceled, expired).

## Input shapes

Every element of `requests` accepts one of:

```python
# 1. Plain string -- wrapped as one user message.
"What is MVCC?"

# 2. Message list -- treated like a regular Messages.create.
[{"role": "user", "content": "What is MVCC?"}]

# 3. Full shape -- you own custom_id + all params.
{
    "custom_id": "eval-case-042",
    "params": {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "..."}],
        "system": "You are terse.",
    },
}
```

Framework-assigned custom_ids follow the pattern `req_0, req_1, ...`.
If you supply your own via shape #3, they're preserved and results
still return in the input order.

## Prompt caching

Batch requests inherit the `enable_prompt_cache=True` setting on the
`Claude` instance. Since batches usually share a system prompt across
all requests, the cache hit rate is high in practice.

## Cost tracking

Per-request `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
and `cache_creation_input_tokens` all funnel into `llm.usage`:

```python
llm.batch(prompts)
print(llm.usage)
# TokenUsage(calls=N, input=..., output=...)
print(f"cache hit ratio: {llm.usage.cache_hit_ratio:.1%}")
```

**Cost estimation caveat:** the standard 50% batch discount is NOT
reflected in `TokenUsage.estimate_cost()` unless you scale the prices
you pass in. Halve `input_price_per_1k` and `output_price_per_1k` when
computing batch cost.

## Timeouts

```python
llm.batch(prompts, poll_interval_sec=30, max_wait_sec=3600)
```

- `poll_interval_sec` = how often to check the batch status. 15-30s
  is reasonable.
- `max_wait_sec` = hard timeout. Anthropic's max batch TTL is 24 hours;
  requests unfinished after that are marked `expired`.

## SDK compatibility

The adapter looks for `client.messages.batches` first, then
`client.beta.messages.batches`. Requires `anthropic>=0.36`. Upgrade if
you see `RuntimeError: This anthropic SDK version does not expose the Batch API`.

## Errors are per-request, not batch-wide

One malformed request doesn't nuke the whole batch. Each failed
request comes back as an error dict; the successful ones return
their text. Check for dicts vs. strings:

```python
results = llm.batch(prompts)
for i, r in enumerate(results):
    if isinstance(r, dict):
        print(f"[{i}] FAILED: {r['type']} -- {r['error']}")
    else:
        print(f"[{i}] {r[:80]}")
```

## When to use vs. sync loop

| Situation | Use |
|---|---|
| < 10 prompts, need low latency | Loop `llm.invoke(...)` |
| 10-100 prompts, can wait a few minutes | `llm.batch(...)` (50% cost cut) |
| Live user conversation | Never batch. Use `invoke` or streaming. |
| Eval harness with 100+ cases | Batch is a huge cost win. |
| Data pipeline overnight | Batch is what it's built for. |

## Non-Claude models

Anthropic-specific. `GPT` doesn't currently have a corresponding
`batch()` method wired -- add one if you need OpenAI batch support
(their API shape is similar; `Files.create` + `Batches.create` +
poll for status).
