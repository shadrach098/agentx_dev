# Prompt caching *(3.1, Claude only)*

Anthropic supports server-side caching of stable prompt segments
(system prompt, tool schemas, long conversation history). Repeat calls
that share those segments read them from cache at ~10% of the input
token cost.

The framework wires this into the `Claude` model. Opt in per instance;
zero code changes elsewhere.

## Enable

```python
from agentx_dev import Claude

llm = Claude(
    model="claude-sonnet-4-6",
    enable_prompt_cache=True,     # 3.1
    cache_history_after=4,        # 3.1: cache long histories after N turns
)
```

That's it. Every subsequent call marks the system prompt and tool
schemas as `cache_control: ephemeral`. Claude serves them from cache on
the next call within 5 minutes.

## What gets cached

Up to 3 cache breakpoints per call (Anthropic allows 4; the framework
reserves one for callers):

1. **System prompt** — always cached when non-empty.
2. **Tool schemas** — the last tool in the list gets the marker, which
   caches the whole tool block.
3. **Conversation history** — when `len(history) > cache_history_after`,
   the last stable assistant message (older than the recent 2 turns)
   gets the marker. Long chats amortize the cost.

Recent user/assistant turns stay UNcached (they change every call).

## Verify it's working

`TokenUsage` surfaces cache stats:

```python
llm.invoke("first call")
u = llm.usage
print(f"cache read tokens: {u.last_cache_read_tokens}")           # 0 on first call
print(f"cache creation tokens: {u.last_cache_creation_tokens}")   # positive on first call

llm.invoke("second call, same system prompt")
u = llm.usage
print(f"cache read tokens: {u.last_cache_read_tokens}")           # positive now
print(f"cache creation tokens: {u.last_cache_creation_tokens}")   # zero

print(f"cumulative cache hit ratio: {u.cache_hit_ratio:.1%}")
```

## Cost math

Anthropic's pricing (as of docs):

| Token class | Price ratio |
|---|---|
| Base input | 1.00x |
| Cache write | 1.25x |
| Cache read | 0.10x |

Break-even after 1 write (cost of writing = ~1 base call). Every
subsequent read saves 90% on those tokens.

For a runner with a 3000-token system prompt hitting the same agent 50
times a session, that's ~150k tokens usually billed as input → billed
as cache reads → ~$0.045 instead of $0.45 at Sonnet pricing.

## Where the biggest wins are

- **Long system prompts** with lots of examples or tool descriptions.
- **Many-tool agents** — cache the tool schemas once.
- **Long-history chatbots** — cache stable history slices with
  `cache_history_after`.
- **Batch/eval runs** — same prompt, many inputs.

## Where it doesn't help

- **First call** — cache miss; you pay 1.25x for the write.
- **Cache expiry** — 5-minute TTL. Sessions with > 5 min gaps between
  calls re-populate.
- **Prompt drift** — any change to the cached segment invalidates.
  Don't dynamically interpolate the current time / user id into the
  system prompt if you can avoid it.

## Non-Claude models

Anthropic-specific. `GPT` doesn't wire prompt caching (OpenAI has its
own auto-caching that requires no code changes, but the framework
doesn't surface stats). Setting `enable_prompt_cache=True` on `Claude`
alone gets you the benefit.

## Under the hood

The framework's cache-preparer helpers in `ChatModel.py`:

- `_prepare_system_for_cache(system_prompt)` — wraps a string system as
  a one-block list with `cache_control: ephemeral`.
- `_prepare_tools_for_cache(tools)` — stamps the last tool spec.
- `_prepare_conversation_for_cache(history)` — stamps the last stable
  assistant message when history is long enough.

Fired on all four Claude call paths (`Initialize`, `async_initialize`,
`call_with_tools`, `async_call_with_tools`) so streaming + tool-calling
+ plain-text all benefit.

## Runnable demo

`examples/v3_1_features_demo.py` — the prompt-caching section shows
first-call vs. second-call token deltas when `ANTHROPIC_API_KEY` is set.
