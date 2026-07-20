# Chat models

Everything the framework says or hears from an LLM goes through a
`BaseChatModel` subclass. Two ship in-tree:

- **`GPT`** — OpenAI Chat Completions API.
- **`Claude`** — Anthropic Messages API.

You can subclass `BaseChatModel` to add any other provider (Bedrock,
Ollama, Together, etc.). The framework never reaches around the model
abstraction, so a new subclass drops in transparently.

## The four things a chat model does

Every subclass implements:

1. **`Initialize(messages) -> str`** — plain text completion. Returns
   assistant text.
2. **`call_with_tools(messages, tools, force_tool=None) -> dict`** —
   native function-calling. Returns either `{"type": "tool_use", "name":
   ..., "input": ..., "id": ..., "tool_calls": [...]}` or `{"type":
   "text", "text": ...}`.
3. **`stream_text(messages) -> Iterator[str]`** — token-level streaming.
4. **`async_initialize`**, **`async_call_with_tools`**, **`astream_text`** —
   async siblings of the above.

`invoke` and `ainvoke` are canonical entry points; they call `Initialize`
under the hood after normalizing the input shape.

```python
from agentx_dev import Claude

llm = Claude(model="claude-sonnet-4-6")

# All three of these work:
reply = llm.invoke("hi")                                     # str
reply = llm.invoke([{"role": "user", "content": "hi"}])      # list
reply = llm.invoke({"messages": [{"role": "user", ...}]})    # dict
```

## GPT — OpenAI

```python
from agentx_dev import GPT

llm = GPT(
    model="gpt-4o",         # or gpt-4o-mini, gpt-4-turbo, etc.
    temperature=0.7,
    max_tokens=2048,
    # Every OpenAI chat.completions.create param is exposed as a kwarg.
)

result = llm.invoke("Explain MVCC in one sentence.")
```

**Gotcha:** reasoning models (`gpt-5.x`, `o1`, `o3`, `o4`) can conflict
with `tools` + `reasoning_effort=medium/high`, causing a 400. Pass
`reasoning_effort="none"` to disable reasoning for that call, or route
through the `/v1/responses` endpoint.

## Claude — Anthropic

```python
from agentx_dev import Claude

llm = Claude(
    model="claude-sonnet-4-6",   # or claude-opus-4-6, claude-haiku-4-5
    max_tokens=4096,
    temperature=1.0,
    enable_prompt_cache=True,    # 3.1: mark system + tools as cacheable
    cache_history_after=4,       # 3.1: cache long histories after N turns
)
```

Prompt caching is Anthropic-specific; see [Prompt caching](../advanced/prompt-caching.md).

## Token usage

Every model instance carries a `TokenUsage` counter:

```python
llm.invoke("...")
llm.invoke("...")
print(llm.usage)
# TokenUsage(calls=2, input=3210, output=587, total=3797)

print(f"spent ${llm.usage.estimate_cost(0.003, 0.015):.4f}")
# spent $0.0184

# 3.1: cache stats (Claude only, non-zero when caching hits)
print(f"cache hit ratio: {llm.usage.cache_hit_ratio:.1%}")
```

Counters accumulate across every call — sync, streaming, and
tool-calling all funnel through the same `_record_usage_counts`
funnel, so nothing goes uncounted.

## Retries + rate limiting + budgets — one call

```python
llm = Claude(model="claude-sonnet-4-6").configure_limits(
    budget_usd=5.0,                # halt when spend crosses $5
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,

    rate_limit_per_sec=5,          # token-bucket: 5 requests/second
    rate_limit_burst=10,           # allow bursts up to 10

    retry_budget=10,               # lifetime cap on retries
)
```

Effects:

- **Rate limit** — every attempt (including retries) counts against the
  token bucket. Runs blocked until a token is available.
- **Retry budget** — retries are capped over the model's whole lifetime.
  When exhausted, `RetryBudgetExceeded` is raised.
- **Cost budget** — after every response with usage data, cumulative
  spend is recomputed. Crossing `budget_usd` raises `CostBudgetExceeded`
  immediately.
- **Non-retryable errors** — 400/401/403/404/422 abort retries after the
  first attempt (they're not transient). 408 (timeout) and 429 (rate
  limit) retry with exponential backoff.

## Structured output

Force the model to fill a Pydantic schema via tool-calling:

```python
from pydantic import BaseModel
from agentx_dev import Claude

class Receipt(BaseModel):
    merchant: str
    total: float
    currency: str = "USD"

extractor = Claude().with_structured_output(Receipt)
receipt = extractor.invoke("Joe's Diner, $12.50 USD")
print(receipt)   # Receipt(merchant="Joe's Diner", total=12.5, currency='USD')
```

Composes with the `|` operator so you can pipe from a prompt template:

```python
pipeline = prompt_template | llm.with_structured_output(Receipt)
receipt = pipeline.invoke({"ocr_text": "..."})
```

See [Structured output](../guides/structured-output.md).

## Streaming

Every model implements `stream_text` (sync) and `astream_text` (async):

```python
for chunk in llm.stream_text([{"role": "user", "content": "Explain MVCC"}]):
    print(chunk, end="", flush=True)
```

For step-level events (thoughts, tool calls, tool results), stream from
the *runner* instead — see [Streaming](../guides/streaming.md).

## Custom providers

Subclass `BaseChatModel` and implement `Initialize`. That alone gives
you sync text; add `call_with_tools` for function-calling and
`stream_text` for streaming.

```python
from agentx_dev import BaseChatModel

class Ollama(BaseChatModel):
    def __init__(self, model="llama3.1"):
        import requests
        self.model = model
        self._session = requests.Session()

    def Initialize(self, messages) -> str:
        response = self._session.post(
            "http://localhost:11434/api/chat",
            json={"model": self.model, "messages": messages, "stream": False},
        )
        self._record_usage_counts(
            input_tokens=response.json()["prompt_eval_count"],
            output_tokens=response.json()["eval_count"],
        )
        return response.json()["message"]["content"]
```

Now `AgentRunner(model=Ollama(), agent=AgentType.ReAct)` works.
