# Guide: streaming

Two kinds of streaming:

1. **Text token deltas** — the LLM's raw output, chunk by chunk.
2. **Agent step events** — thoughts, tool calls, tool results, final.

You usually want both.

## Model-level text streaming

Every `BaseChatModel` implements `stream_text` (sync) and `astream_text`
(async):

```python
from agentx_dev import Claude

llm = Claude()

for chunk in llm.stream_text([{"role": "user", "content": "Explain MVCC"}]):
    print(chunk, end="", flush=True)
```

Async:

```python
async for chunk in llm.astream_text([{"role": "user", "content": "Explain MVCC"}]):
    print(chunk, end="", flush=True)
```

**Usage capture** — both Claude and GPT implementations record the
final usage block from the stream (via `stream_options` for OpenAI, via
`get_final_message()` for Anthropic) so `llm.usage` stays accurate for
streamed calls.

## Runner-level event streaming

`runner.stream(...)` yields structured step events:

```python
runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[weather_tool])

for event in runner.stream("What's the weather in NYC?"):
    kind = event["type"]
    if kind == "thought":
        print("[thinking]", event["content"])
    elif kind == "tool_call":
        print(f"[call] {event['name']}({event['args']})")
    elif kind == "tool_result":
        print(f"[result] {event['result'][:80]}")
    elif kind == "final":
        print(f"\n[answer] {event['content']}")
    elif kind == "completion":
        full = event["completion"]   # AgentCompletion
```

The LAST event is always `{"type": "completion", "completion": ...}` —
callers that need both real-time updates AND the final aggregate can
consume it in one loop.

## Both at once — text_delta + step events

Add `stream_tokens=True`:

```python
for event in runner.stream("Explain MVCC", stream_tokens=True):
    if event["type"] == "text_delta":
        print(event["content"], end="", flush=True)
    elif event["type"] == "tool_call":
        print(f"\n[calling {event['name']}]")
    elif event["type"] == "tool_result":
        print(f"[got {len(event['result'])} chars]")
    elif event["type"] == "final":
        print("\n\nDONE")
```

`text_delta` events only fire when `use_function_calling=False` (in
function-calling mode the model emits structured tool calls, not
streaming text). Non-streaming models (or subclasses that don't
override `stream_text`) yield the whole response in one chunk.

## Async streaming

`AsyncAgentRunner.astream(...)` mirrors sync:

```python
from agentx_dev import AsyncAgentRunner, AgentType, Claude

runner = AsyncAgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

async for event in runner.astream("Explain MVCC", stream_tokens=True):
    if event["type"] == "text_delta":
        print(event["content"], end="", flush=True)
```

## Streaming to a web client (FastAPI + SSE)

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

@app.post("/chat")
async def chat(request: dict):
    runner = build_runner()

    async def event_stream():
        async for event in runner.astream(request["query"], stream_tokens=True):
            # Skip the final completion event — it's the same AgentCompletion
            # object; the caller can reconstruct it from the streamed pieces.
            if event["type"] == "completion":
                continue
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

## Streaming through Sessions

`Session.invoke` doesn't stream (it absorbs the completion). To stream
AND persist, drive the runner directly and save the session after:

```python
session = Session.start(runner)

# Reconstruct chat_history from the session on each call:
completion = None
for event in runner.stream(user_input, chat_history=session.history):
    yield event   # to your client
    if event["type"] == "completion":
        completion = event["completion"]

session._absorb(completion)
session.save("./sessions/chat.json")
```

## Streaming utilities

`agentx_dev.Streaming` ships helpers:

- **`StreamBuffer`** — accumulate a stream and get the joined text at
  the end. Useful when you want to display live tokens but also do
  post-processing on the whole answer.
- **`StreamProcessor`** — apply a callable to each chunk (e.g. syntax
  highlighting, markdown rendering, redaction).
- **`OpenAIStreamAdapter`** — normalize the OpenAI SDK's chunk format
  into `StreamChunk` objects.
- **`simple_stream(model, messages)`** — convenience wrapper that yields
  strings.

## Common issues

- **Nothing prints** — check `flush=True` in your `print(...)`.
- **Streaming and function-calling** — mutually incompatible for
  `text_delta` events; the model emits structured tool calls instead
  of streaming text.
- **Chunks arrive in bursts** — provider buffering. Try adjusting the
  provider's `stream_options`.
- **Slow first token** — that's the model's warmup, not the framework.
  Claude's TTFT is typically 500-1500ms.
