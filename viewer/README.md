# AgentX Trace Viewer

Self-hosted timeline for AgentX observability traces. Reads a JSONL file
produced by `FileHook`, renders every event with duration, type filter,
text search, and per-event JSON drill-down.

## Open it

Just double-click `viewer/index.html`. No server required.

## Produce a trace

Wire the built-in `FileHook` to your agent:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    observability, FileHook, config,
)

config.observability_enabled = True
observability.add_hook(FileHook("./trace.jsonl"))

runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
)
result = runner.invoke("...")
```

Then open `viewer/index.html` and drop `trace.jsonl` on it (or use the
file picker).

## What you get

- **Timeline** -- every event as a row, colored by kind (agent / LLM /
  tool / error). Click a row to see the full JSON.
- **Summary sidebar** -- event count, agent runs, tool calls, error
  count, wall clock, cumulative input/output tokens, cache reads.
- **Type filter** -- click a type in the sidebar to hide it. Click again
  to show. Multiple types togglable.
- **Text filter** -- substring match across type + data. Case-insensitive.
- **Relative timestamps** -- every event shows its offset from the first
  event (0s, 0.42s, 1.20s, ...).

## Event shape it expects

Anything with `type` and (optionally) a `data` dict works. Recognized
fields:

| Field | Purpose |
|---|---|
| `type` | Event name (e.g. `TOOL_CALL_START`, `AGENT_END`) -- decides color |
| `ts` / `timestamp` / `time` | Numeric unix seconds OR ISO 8601 |
| `data.tool_name` | Displayed for tool events |
| `data.query` | Displayed for agent_start |
| `data.final_answer` | Displayed for agent_end |
| `data.input_tokens` / `data.output_tokens` | Roll up into summary |
| `data.cache_read_tokens` | Roll up into summary |
| `data.duration_ms` | Rendered in the right-hand column |
| `data.is_error` | Marks the row red (in addition to `type` containing `error`) |

Unknown fields are ignored but still visible in the click-through JSON.

## Wins vs. LangSmith

- **Self-hosted**, no account, no upload.
- **Works from `file://`** -- no server, no build step.
- **JSONL source of truth** -- one plain-text file per run, easy to
  archive, grep, diff.

## Limits

- Not a distributed tracing UI. Not OTel-native. If you need that,
  register `OTelHook` alongside `FileHook`.
- No timeline zoom / pan for very long runs (thousands of events).
- No cost aggregation with pricing (would need the pricing table).
