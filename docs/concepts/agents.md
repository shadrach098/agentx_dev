# Agents

An **agent** in AgentX is a model + a prompt template + a set of tools +
a control loop. The class is `AgentRunner` (sync) or `AsyncAgentRunner`
(async). This doc covers:

1. Agent types (prompt templates)
2. Parsers (StandardParser and its variants)
3. The runner loop step-by-step
4. Modes: text mode, function-calling mode, native-binding mode

## Agent types (prompt templates)

Every agent type is a prompt template plus a Pydantic parser that
matches the shape the template asks the model to emit. Five ship:

| AgentType | Template style | Parser fields | When to use |
|---|---|---|---|
| `ReAct` | Thought / Action / Observation | `Thought`, `action`, `action_input` | Default; explicit reasoning |
| `Chain_of_Thought` | Reason, then act | `Thought`, `Action`, `Action_Input` | Emphasizes reasoning depth |
| `Zero_Shot` | Minimal scaffolding | `action`, `action_input` | Simple deterministic tasks |
| `Few_Shot` | Includes examples | `action`, `action_input` | Learning from demonstrations |
| `Instruction_Tuned` | Direct commands | `action`, `action_input` | Instruction-following models |

All follow a unified shape internally: **HARD RULES → PROCESS
checklist → ANTI-PATTERNS → OUTPUT CONTRACT** with worked examples.

Pick with the `AgentType` enum:

```python
from agentx_dev import AgentRunner, AgentType, Claude

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])
```

## Parsers

Each template has a Pydantic model that specifies the shape the LLM's
response must fit. The runner calls `parser.from_json(response)` — if
the response parses to a parser instance, the runner extracts `action`
and `action_input` from it. If parsing fails, the response is treated
as a plain-text final answer.

You can build your own template + parser pair by subclassing
`AgentFormatter`:

```python
from pydantic import BaseModel
from agentx_dev import AgentFormatter, AgentRunner

class MyParser(BaseModel):
    action: str
    action_input: str

MY_TEMPLATE = """You have these tools:
{tools}

Names: {tool_names}

User: {user_input}

Respond with JSON: {{"action": "<tool>", "action_input": "<args>"}}
"""

runner = AgentRunner(
    model=Claude(),
    agent=AgentFormatter(prompt=MY_TEMPLATE, Agent=MyParser),
)
```

The template MUST contain `{tools}`, `{tool_names}`, and `{user_input}`
placeholders — the runner validates this at construction.

## The runner loop

```
                       +----------------------+
                       | build system prompt  |
                       +----------+-----------+
                                  v
                       +----------+-----------+
                       | append user_input    |
                       +----------+-----------+
                                  v
        +-------------------------+-------------------------+
        |                                                   |
        v                                                   |
  +-----+------+   final answer                             |
  | LLM call   +--------------->  return AgentCompletion    |
  +-----+------+                                            |
        |                                                   |
        | parser returns (action, action_input)             |
        v                                                   |
  +-----+------+                                            |
  | dispatch   |                                            |
  | tool via   |                                            |
  | registry   |                                            |
  +-----+------+                                            |
        |                                                   |
        | append tool result                                |
        +---------------------------------------------------+
                                  (up to max_iterations)
```

Every iteration is one LLM call. `max_iterations` caps the loop. When
the LLM emits `action=Final_Answer` (or any tolerant variant like
"Final Answer", "final_answer"), the loop terminates with
`action_input` as the final answer.

Framework guards fire automatically:

- **Duplicate-call detection** (tool layer) — warns at 3 identical
  calls, refuses at 5.
- **Loop-level force-stop** — 3 consecutive identical `(action, args)`
  pairs and the loop bails with a synthesized final answer from the
  last successful tool result.
- **Implicit-final** — when `action` is neither a known tool nor a
  Final_Answer variant, the framework treats `action_input` as the
  final answer (never `Thought` — that's internal reasoning).

## Three runner modes

**1. Text mode** (default) — parser JSON in the assistant message.

```python
runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[calc])
# Model emits: {"Thought": "...", "action": "calculator", "action_input": {...}}
```

**2. Function-calling mode** — parser routed through native FC.

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[calc],
    use_function_calling=True,
)
# Model calls the parser tool with structured args.
```

**3. Native-binding mode** (3.1) — YOUR tools are the native FC tools.

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[calc, weather],
    bind_tools_natively=True,
    parallel_tool_workers=4,   # concurrent dispatch when the model
                               # calls multiple tools per turn
)
# Model calls calculator / weather / respond as real FC tools.
# No parser scaffolding — see 3.1 caveats below.
```

`bind_tools_natively=True` and `use_function_calling=True` are mutually
exclusive.

**When to use which:**

- **Text mode** — most compatible; works with any model; good for
  cross-provider portability.
- **Function-calling mode** — parser output is structured, so schema
  validation is stronger; slightly higher accuracy on tool-call
  correctness.
- **Native binding** — best latency (parallel tool calls per turn),
  simplest prompt for the model. Use when your tools are well-described
  and you don't need the ReAct scaffold. The framework auto-adds a
  `respond` tool the model calls to end the loop.

## The AgentCompletion object

Every `runner.invoke(...)` returns an `AgentCompletion` with:

```python
result.content         # str  — final answer
result.tool_calls      # List[ToolCall] — every dispatched call
result.steps           # List[str] — step descriptions like "Step 1: get_weather with Paris"
result.history         # List[Dict] — the full working_history
result.query           # str  — the original user input
result.model           # str  — the model class name
result.id              # str  — uuid
result.created         # int  — unix timestamp
result.output          # Any  — set when output_schema= was passed
```

`ToolCall` fields:
```python
tc.name    # tool name
tc.args    # {"Input": <what the model passed>}
tc.result  # str — the tool's return value (stringified)
```

## Streaming events

`runner.stream(query)` yields structured step events:

```python
for event in runner.stream("What's the weather in NYC?"):
    if event["type"] == "thought":       print("Thought:", event["content"])
    elif event["type"] == "tool_call":   print("Calling", event["name"])
    elif event["type"] == "tool_result": print("Got:", event["result"])
    elif event["type"] == "final":       print("Final:", event["content"])
    elif event["type"] == "completion":  full = event["completion"]  # AgentCompletion
```

Add `stream_tokens=True` for `text_delta` events with per-token chunks.
See [Streaming](../guides/streaming.md).

## Chat history

Pass prior turns as `chat_history`:

```python
result = runner.invoke("What about tomorrow?", chat_history=[
    {"role": "user", "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": "22C and sunny."},
])
```

Or pass a message list directly to `invoke` and the framework will pull
the last user turn as the query and thread earlier turns as history:

```python
result = runner.invoke([
    {"role": "user", "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": "22C and sunny."},
    {"role": "user", "content": "And tomorrow?"},
])
```

## Async

`AsyncAgentRunner` has the same API but every method is async:

```python
from agentx_dev import AsyncAgentRunner, AgentType, Claude

runner = AsyncAgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[calc])
result = await runner.ainvoke("What is 12 * 47?")

async for event in runner.astream("Do X"):
    ...
```

Async supports mixed sync + async tools transparently. See [Streaming](../guides/streaming.md) for the async pattern.

## Common gotchas

- **`use_function_calling=True` + `bind_tools_natively=True`** — raises
  at construction. Pick one.
- **Passing a list-of-messages to `invoke`** — the framework normalizes
  it. The last user turn becomes the query; earlier turns become chat
  history. System messages in the list are dropped (the runner builds
  its own).
- **`max_iterations` default is 4** — bump for complex tasks.
- **Empty `tool_calls` on completion** — the model produced a direct
  text answer (either no tools were needed, or it went straight to
  Final_Answer).
