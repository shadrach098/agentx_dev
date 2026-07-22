# Trace viewer

`FileHook` writes every observability event to a JSONL file. `viewer/`
is a self-hosted single-page app that reads that file and renders a
timeline -- summary sidebar, filterable events, per-event JSON
drill-down.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## Produce a trace

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

Every `AGENT_START`, `LLM_CALL_END`, `TOOL_CALL_START/END`, `TOOL_ERROR`,
`CIRCUIT_OPEN`, `CACHE_HIT`, `RETRY`, `MEMORY_UPDATE` event lands as
one line in `trace.jsonl`.

## Open the viewer

Double-click `viewer/index.html`. Drop `trace.jsonl` onto the page (or
use the file picker).

## What you get

- **Timeline** -- every event as a row, colored by kind (agent / LLM /
  tool / error). Click a row for the full JSON.
- **Summary sidebar** -- event count, agent runs, tool calls, errors,
  wall clock, total input/output tokens, cache reads.
- **Type filter** -- click a type in the sidebar to hide it.
- **Text filter** -- substring match across type + data.
- **Relative timestamps** -- offsets from the first event.

## Event fields the viewer recognizes

Unknown fields are ignored (but visible in the click-through JSON).

| Field | Role |
|---|---|
| `type` | Event name; decides color |
| `ts` / `timestamp` / `time` | Unix seconds OR ISO 8601 |
| `data.tool_name` | Shown for tool events |
| `data.query` | Shown for `AGENT_START` |
| `data.final_answer` | Shown for `AGENT_END` |
| `data.input_tokens`, `data.output_tokens`, `data.cache_read_tokens` | Roll into summary |
| `data.duration_ms` | Right-column duration |
| `data.is_error` | Marks the row red |

## Not LangSmith, and that's the point

- **Self-hosted** -- no account, no upload, no third party.
- **`file://` works** -- no server required to view.
- **Plain-text source** -- JSONL is easy to grep, diff, archive.

Trade-offs:
- No distributed tracing UI. For that, use `OTelHook` alongside
  `FileHook` and view in your OTel backend.
- No timeline zoom/pan for runs with thousands of events. The filter
  handles this in practice.
- No cost aggregation with pricing table. Use `TokenUsage.estimate_cost`
  in your code.

## Serving from HTTP (optional)

```bash
python -m http.server -d viewer 8001
# then open http://localhost:8001
```

Nicer URLs, same behavior.
