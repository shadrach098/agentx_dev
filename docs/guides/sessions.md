# Guide: sessions

Without persistence, every process restart loses conversation state.
`Session` serializes the full history to disk so an agent picks up
exactly where it left off.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## What gets saved

- Full message history (with provider-native tool_use IDs preserved)
- Every tool call the agent made across all invokes
- Token usage totals
- Per-completion metadata (id, model, query, content, steps)
- Free-form metadata dict (tenant id, user email, whatever you stash)
- Schema version + created/updated timestamps

## What does NOT get saved

- The runner (holds live API clients + tool closures)
- The model (holds SDK state)
- Tools themselves

Loading a session requires reattaching a runner.

## Fresh session

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions, Session

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./work"]),
)

session = Session.start(runner, metadata={"user_id": "alice"})
session.invoke("Plan a 3-day trip to Paris.")
session.save("./sessions/paris-trip.json")
```

## Resume after restart

```python
runner = AgentRunner(...)   # same shape as before

session = Session.load("./sessions/paris-trip.json", runner=runner)
session.invoke("Add a stop in Versailles on day 2.")
session.save("./sessions/paris-trip.json")
```

Or chain:

```python
session = Session.load("./sessions/paris-trip.json").attach(runner)
```

Forgetting `.attach(runner)` raises a clear `RuntimeError` on the next
invoke rather than crashing inside the loop.

## Async

```python
session = Session.start(runner)
await session.ainvoke("...")
session.save("./sessions/x.json")
```

Requires `AsyncAgentRunner` (or any runner exposing `.ainvoke`).

## Atomic save

`session.save(path)` writes to `path.tmp` then renames — a crashed
process can't leave a half-written session file.

## Version migration

Every save records `version: 1`. Loading a file with a newer version
than this build knows raises `ValueError`. Older versions load with an
opportunity to migrate — inspect `data["version"]` before constructing.

## Serialization format

JSON, with `default=str` fallback for non-JSON types. Programs can
inspect the file directly:

```json
{
  "session_id": "b1e5...",
  "version": 1,
  "created_at": "2026-07-19T13:00:00+00:00",
  "updated_at": "2026-07-19T13:10:00+00:00",
  "model_class_name": "Claude",
  "agent_class_name": "React_",
  "history": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "tool_calls_log": [
    {"name": "read_path", "args": {"Input": "./work/x.md"}, "result": "..."}
  ],
  "completions_meta": [
    {"id": "c1...", "created": 1737288000, "model": "Claude", ...}
  ],
  "usage": {"input_tokens": 3210, "output_tokens": 587, "calls": 3},
  "metadata": {"user_id": "alice"}
}
```

## Common patterns

### Multi-user chatbot

One session per user, keyed by user_id:

```python
def session_path(user_id):
    return Path(f"./sessions/{user_id}.json")

def chat(user_id: str, message: str) -> str:
    runner = build_runner()
    path = session_path(user_id)
    session = (
        Session.load(path).attach(runner) if path.exists()
        else Session.start(runner, metadata={"user_id": user_id})
    )
    completion = session.invoke(message)
    session.save(path)
    return completion.content
```

### Resume with a different agent type

Load a session that was created with an Instruction_Tuned runner into a
ReAct runner. The stored `agent_class_name` is a diagnostic only — the
new runner takes over.

### Inspect without resuming

```python
session = Session.load("./sessions/x.json")   # no runner needed
print(f"Turns: {len(session.history) // 2}")
print(f"Total spent tokens: {session.usage['input_tokens'] + session.usage['output_tokens']}")
print(f"Tools used: {[tc['name'] for tc in session.tool_calls_log]}")
```

Any state-changing method (`invoke`, `ainvoke`) still requires an
attached runner.

## Runnable example

See `examples/chatbot_example.py` — multi-turn chat with persistent
sessions across restarts.
