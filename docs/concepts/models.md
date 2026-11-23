# Chat models

Everything the framework says or hears from an LLM goes through a
`BaseChatModel` subclass. Three ship in-tree:

- **`GPT`** — OpenAI Chat Completions API.
- **`Claude`** — Anthropic Messages API.
- **`Local`** — a server on your machine or network that speaks the OpenAI chat-completions protocol: Ollama,
  LM Studio, llama.cpp. See [Local models](#local-models).

You can subclass `BaseChatModel` to add any other provider (Bedrock,
Together, etc.). The framework never reaches around the model
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
    model="claude-opus-5-5",     # the default; or claude-sonnet-5-5, claude-haiku-5-5, claude-fable-5-1
    max_tokens=16000,            # the default; thinking counts toward it on models that always think
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
to each provider's format. See [Media](../media/overview.md).

```python
from agentx_dev import Media
llm.invoke("What's this?", media=["cat.jpg"])                                  # 3.4.2
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

## Local models

`Local` talks to any server that offers OpenAI's chat-completions address. Ollama is the default
(`http://localhost:11434/v1`); LM Studio is `http://localhost:1234/v1`; llama.cpp's server works the same way.

```python
from agentx_dev import AgentRunner, AgentType, Local

llm = Local(model="qwen3:8b")                                          # Ollama on this machine
# llm = Local(model="qwen3:8b", base_url="http://localhost:1234/v1")   # LM Studio: its own address

check = llm.check()                 # the server answers, has the model, and the model calls a tool
if not check.ok:
    raise SystemExit(check.reason)  # e.g. "the local server at localhost:11434 is not answering"

runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[...])
```

What `Local` does that `GPT(base_url=...)` does not:

- **No key.** It sends a placeholder, and never reads `OPENAI_API_KEY`: that key must not be sent to a local or
  third-party server. A server that wants a key gets `api_key=`.
- **Chat completions only.** Never the Responses API.
- **No `model`:** it uses the server's only model; when the server has several it raises `LocalModelError` naming
  them. `Local.models(base_url=...)` lists what a server has, without building a model. An address with no
  `http://` gets it (`localhost:1234` works).
- **`check()`** returns a `LocalCheck(ok, reason, context_window)`. It asks the server for its models, looks for
  yours, and sends one tiny request with one tool: a model that answers in text instead of calling the tool
  "cannot drive agentx's tools". The first request can take a while (the server loads the model); the check waits
  up to two minutes. On Ollama it also reads the window the model is loaded with.
- **Native tool binding, chosen for you.** Small local models fail the ReAct text format, so an `AgentRunner` on a
  `Local` binds the tools natively without being told. An explicit `bind_tools_natively=False` still wins.
- **Thinking is kept.** What Qwen and other reasoning models put in `reasoning` (Ollama) or `reasoning_content`
  (LM Studio, vLLM) comes back as `"reasoning"` and is shown as thinking, as for Claude and GPT. It is never sent
  back to the model as part of its answer.
- **Tokens** are counted from the server's `usage`. There is no price table, so there is no cost.

### The context window

A local server often loads a model with a small window (Ollama's default is often 8,192 tokens), and when a
request is longer than the window it silently drops the start of it, which is where the instructions are. So the
runner keeps every request inside the window when it knows it:

```python
runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[...], context_window=16384)
```

`context_window=` defaults to the model's own (`Local` learns it from `check()`, or take
`Local(context_window=...)`). Each request may use 85% of it; the rest is for the answer. When a request would be
longer, these steps run in order, and it stops as soon as the request fits:

1. old tool results are replaced by one-line stubs (the newest kept whole);
2. the conversation is summarised in place;
3. the newest oversized tool result is cut to the room left (its start and end kept);
4. if even the instructions and the task message do not fit, the request is **not sent** and the run raises
   `ContextWindowTooSmall` (`window`, `needed`).

`stream` yields `{"type": "context_trimmed", "window", "how"}` (`how` is `"results"` or `"summary"`) the first
time results are stubbed or the conversation is summarised in a run.

A bigger window is a change on the server, not in your code. For Ollama, make a variant of the model with a larger
`num_ctx`:

```text
# Modelfile
FROM qwen3:8b
PARAMETER num_ctx 16384
```

```bash
ollama create qwen3-16k -f Modelfile
```

then use `Local(model="qwen3-16k")`. A larger window uses more memory.

### A fallback model

Name a model to take over when yours fails for good:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Local

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[...],
                     fallback_model=Local(model="qwen3:8b"))
```

"Fails for good" means waiting will not fix it: no credit (`credit balance is too low`, `insufficient_quota`), a
rejected key (401, 403), an unknown model (404), or a provider that cannot be reached (a connection error, a
timeout, a 5xx) after the client's own retries are used up. A rate limit, a bad request, a tool error and the
framework's own stops (budget, stuck) are not: those are retried or raised as they were.

When one happens, the runner:

- carries the run on from where it stopped (tool calls already made are not made again), in the binding mode the
  fallback needs, with a note telling it what happened;
- yields `{"type": "fallback", "from", "to", "reason"}` from `stream` and logs a warning;
- stays on the fallback for the rest of its life (`runner.fallback_used` holds the switch) until
  `runner.reset_fallback()`;
- marks the run: `completion.fallback` is `{"from", "to", "reason"}` (None for a run on the model you built the
  runner with);
- raises `FallbackFailed` (import it from `agentx_dev.Runner.Fallback`) if the fallback fails too: the message
  gives both reasons and the fallback's own error is the `__cause__`.

A `Local` model never falls back to anything. `Supervisor(..., fallback_model=Local(...))` gives the same
fallback to each helper it builds. The fallback belongs to the sync `AgentRunner`; `AsyncAgentRunner` does not
have it yet.

**Persistent runs never fall back on "unreachable".** With `persistence=`, a connection error, a timeout or a 5xx is
a transient error: the run waits it out with growing pauses until its time budget is spent, and then ends with
the outcome `out_of_time` (the budget expired), not on the fallback. A rejected key, no credit and an unknown model still
switch at once, as above.

**Instructions for the fallback.** A small local model may not fit the instructions written for Claude.
`fallback_addendum=` gives the `system_addendum` to use once the run has switched: a string, or a function that gets
the fallback model and returns one (so it can read `fallback.context_window`). The sandbox hint and the skill index
stay; `reset_fallback()` puts the first addendum back.

**How much of a window is already taken.** `runner.fixed_request_chars(model=None)` is the characters every request
on that model carries before any conversation: the system prompt for the binding mode it gets and the function specs
that mode sends. Divide by about 4 for tokens and compare with 85% of the window to see what is left.

### An empty answer is not an answer

For every model, a run that calls no tool and gives no answer ends with the outcome `no_answer` (its text is "The
model gave no answer."), not `done`. A run that did tool work and then ended with nothing stays `done` (the work
happened), and its answer is a note: "The model gave no final answer. It called: read_path, write_file (files
changed: done.txt)." Small local models do this now and then, which is why it matters.

## Custom providers

Subclass `BaseChatModel` and implement `Initialize`. That alone gives
you sync text; add `call_with_tools` for function-calling and
`stream_text` for streaming.

```python
from agentx_dev import BaseChatModel

class Bedrock(BaseChatModel):
    def __init__(self, model="anthropic.claude-sonnet"):
        import boto3
        self.model = model
        self._client = boto3.client("bedrock-runtime")

    def Initialize(self, messages) -> str:
        response = self._client.converse(
            modelId=self.model,
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]}
                      for m in messages if m["role"] != "system"],
        )
        usage = response["usage"]
        self._record_usage_counts(
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"],
        )
        return response["output"]["message"]["content"][0]["text"]
```

Now `AgentRunner(model=Bedrock(), agent=AgentType.ReAct)` works. A server that speaks the OpenAI protocol
(Ollama, LM Studio, vLLM, llama.cpp) needs no subclass: use `Local`.
