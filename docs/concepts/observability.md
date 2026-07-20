# Observability

The framework emits structured events for every meaningful step so you
can trace, log, monitor, or replay agent runs.

## Event types

| Event | Fired when |
|---|---|
| `AGENT_START` | Runner begins a new invoke |
| `AGENT_END` | Runner returns an AgentCompletion |
| `LLM_CALL_START` | Before a chat model API call |
| `LLM_CALL_END` | After a chat model API call (with token counts) |
| `TOOL_CALL_START` | Before a tool dispatch |
| `TOOL_CALL_END` | After a tool dispatch (with result) |
| `TOOL_ERROR` | Tool dispatch returned or raised a ToolError |
| `RETRY` | An LLM call is being retried after failure |
| `CACHE_HIT` | A tool call was served from the framework's tool cache |
| `CIRCUIT_OPEN` | A tool's circuit breaker tripped |
| `MEMORY_UPDATE` | Auto-memory added a message |

Every event carries `data: dict` with type-specific fields plus
timestamps. See `agentx_dev.Observability.EventType`.

## Turn observability on

```python
from agentx_dev import config

config.observability_enabled = True    # off by default
```

Or set the env var:

```bash
export AGENTX_OBSERVABILITY=1
```

## Built-in hooks

Register a hook to receive every event:

```python
from agentx_dev import observability, ConsoleHook, FileHook

# Pretty-print to stdout with timestamps.
observability.add_hook(ConsoleHook(verbose=True))

# Also append to a JSON-lines file.
observability.add_hook(FileHook("./agent_events.jsonl"))
```

Ship names:

| Hook | Purpose |
|---|---|
| `ConsoleHook(verbose=False)` | Pretty print to stdout |
| `FileHook(path)` | Append JSONL to a file (atomic-per-line write) |
| `MetricsHook(callback)` | Bucket events into Prometheus-style counters |
| `CallbackHook(fn)` | Route to any `(event) -> None` callable |
| `OTelHook(tracer)` | OpenTelemetry span emitter (needs `[otel]` extra) |

## Custom hook

Subclass the abstract `Hook` or use `CallbackHook`:

```python
from agentx_dev import observability, CallbackHook

def to_datadog(event):
    if event.type.name.startswith("TOOL"):
        # your datadog client
        datadog.increment(f"agent.{event.type.name.lower()}",
                          tags=[f"tool:{event.data.get('tool_name', '?')}"])

observability.add_hook(CallbackHook(to_datadog))
```

## Redaction

Tool args and results are passed through `redact_secrets` before being
logged. The default redactor catches:

- API-key-shaped strings (`sk-`, `ANTHROPIC_*`, JWT, bearer tokens)
- OAuth secrets
- AWS access keys

Add your own patterns:

```python
from agentx_dev import redact_secrets, config
config.redaction_patterns.append(r"my-secret-[a-z0-9]+")
```

## OpenTelemetry

```python
from agentx_dev import observability, OTelHook
from opentelemetry.trace import get_tracer

tracer = get_tracer("agentx")
observability.add_hook(OTelHook(tracer))
```

Each `AGENT_START` opens a span; `LLM_CALL_START` and `TOOL_CALL_START`
open child spans that close on their matching END events. Token usage
lands as span attributes (`llm.input_tokens`, `llm.output_tokens`).

## Tracking custom tool calls

Use `track_tool_call` / `track_async_tool_call` decorators to add
observability to non-framework code paths:

```python
from agentx_dev import track_tool_call

@track_tool_call(name="external_api_call")
def fetch_prices(symbol):
    ...
```

## Turning specific events off

```python
config.observability_events = {
    "AGENT_START", "AGENT_END",
    "TOOL_CALL_START", "TOOL_CALL_END",
    # everything else off
}
```

Default: all events on.
