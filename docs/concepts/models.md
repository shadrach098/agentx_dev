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
reply = llm.invoke({"messages": [{"role": "user", "content": "hi"}]})  # dict
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

`reasoning_effort` takes any value a model generation uses —
`"none"`, `"minimal"`, `"low"`, `"medium"`, `"high"`, `"xhigh"`.
Generations disagree about which are valid, and about whether they
take the parameter at all; see [Old and new models](#old-and-new-models).

## Claude — Anthropic

```python
from agentx_dev import Claude

llm = Claude(
    model="claude-sonnet-4-6",   # or claude-opus-4-6, claude-haiku-4-5
    max_tokens=4096,
    enable_prompt_cache=True,    # 3.1: mark system + tools as cacheable
    cache_history_after=4,       # 3.1: cache long histories after N turns
    # 3.4, all optional and only sent when set:
    # temperature=0.3, top_p=0.9, top_k=40, stop_sequences=["END"],
    # thinking={"type": "enabled", "budget_tokens": 8000},
)
```

`temperature` defaults to `None` since 3.4 — not sent, so the API
default (1.0, the old default) applies. Sending it unconditionally
collided with `top_p` on newer models and with extended thinking.
With `thinking` set, `temperature` and `top_k` are held back because
Anthropic requires the defaults there. Thinking with native tool
calling isn't supported yet (thinking blocks aren't replayed into
later turns).

Prompt caching is Anthropic-specific; see [Prompt caching](../advanced/prompt-caching.md).

## Old and new models

Request parameters drift between model generations, and a value one
model needs is a 400 on another:

| Parameter | What varies |
|---|---|
| `reasoning_effort` | absent on `gpt-4o`-era models; `low`–`high` on `o1`/`o3`/`o4`; `minimal`–`high` on `gpt-5`; `none`–`high` on `gpt-5.1`; some newer models add `xhigh` and drop `none` |
| `max_tokens` | reasoning models reject it and want `max_completion_tokens` |
| `temperature` | reasoning models accept only the default |
| Claude `top_p` | newer models reject it alongside `temperature` |
| Claude `thinking` | older models reject it |
| Claude `max_tokens` | capped per model |

Rather than a lookup table that goes stale the day a model ships, both
`GPT` and `Claude` read the provider's 400 — which names the parameter
and, for enums, the allowed values — make the smallest change that
satisfies it, retry, and **remember the change for that model** so
later calls never pay the rejected request again:

```python
llm = GPT(model="gpt-5.4", reasoning_effort="none")
llm.invoke("hi")
# WARNING  OpenAI model 'gpt-5.4' rejected a request parameter; changed
#          reasoning_effort 'none' -> 'low' and retried. Remembered for
#          this model.
```

The adjustments:

- **Unsupported enum value** → the nearest value the model lists, on
  the `none < minimal < low < medium < high < xhigh` scale.
- **Renamed parameter** ("Use 'max_completion_tokens' instead") →
  sent under the new name. Known reasoning families get this up front
  with no rejected call.
- **Unsupported parameter** → dropped, so the model uses its default.
- **Claude `max_tokens` over the cap** → clamped to the cap.

Only parameter-compatibility errors are handled. Anything else — a
context-length overflow, a bad API key — raises exactly as before, and
`model`, `messages`, and `tools` are never altered. Each adjustment is
logged at `WARNING`. For the raw provider error, opt out:

```python
GPT(model="gpt-5.4", reasoning_effort="none", adapt_params=False)
Claude(temperature=0.3, top_p=0.9, adapt_params=False)
```

### Tools on models that need the Responses API

Some OpenAI models refuse function tools combined with reasoning on
`/v1/chat/completions`:

```
Function tools with reasoning_effort are not supported for gpt-6-astra in
/v1/chat/completions. To use function tools, use /v1/responses or set
reasoning_effort to 'none'.
```

When the model also rejects `'none'`, the Responses API is the only way to
give it tools. `GPT` switches that model's tool calls to `/v1/responses`
when the provider says so, retries, and remembers it:

```
WARNING  OpenAI model 'gpt-6-astra' can't call tools on /v1/chat/completions
         with the current settings; switching its tool calls to the Responses
         API (/v1/responses) and retrying. Remembered for this model.
```

Everything else stays the same: the agent's history, `tool_calls`, and
`completion` look identical, learned parameter fixes (like `'none'` →
`'low'`) carry over, and plain text calls and token streaming stay on
chat completions. Requests go out with `store=False`, since the Responses
API stores requests on OpenAI's side by default and chat completions
doesn't. Chat-only settings with no Responses equivalent (`seed`, `stop`,
`n`, penalties, `logit_bias`) aren't sent on that endpoint; a WARNING
names them once.

```python
GPT(model="gpt-6-astra")                           # automatic (default)
GPT(model="gpt-6-astra", use_responses_api=True)   # always use /v1/responses
GPT(model="gpt-6-astra", use_responses_api=False)  # never; raise the provider error
```

## Images, PDFs, audio

Both models accept media in a message's `content` list — a `Media`
object, an OpenAI-style part, or an Anthropic-style block, translated
to each provider's format. See [Media](../guides/media.md).

```python
from agentx_dev import Media
llm.invoke([{"role": "user", "content": ["What's this?", Media.image("cat.jpg")]}])
```

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
