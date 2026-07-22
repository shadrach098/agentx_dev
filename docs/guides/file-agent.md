# Guide: build a file-editing agent

Goal: an agent that reads a file, improves the writing, saves the
improved version, and reports what changed.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## Minimum viable

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(model="claude-sonnet-4-6"),
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True,
        write_files=True,
        edit_files=True,
        allowed_paths=["./workspace"],
    ),
    max_iterations=8,
)

result = runner.invoke(
    "Read ./workspace/draft.md. Improve it for clarity and brevity. "
    "Save the improved version to ./workspace/draft_v2.md. "
    "Tell me three specific edits you made."
)

print(result.content)
for tc in result.tool_calls:
    print(f"  {tc.name}({tc.args})")
```

The framework registers `read_path`, `write_file`, `edit_file` because
you granted those capabilities. Denied tools (`delete_files`,
`execute_python`, etc.) aren't visible to the model.

## Production variant

Add a cost cap, a circuit breaker, and session persistence:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    Session, ToolRegistry, CircuitBreakerConfig,
)

llm = Claude(model="claude-sonnet-4-6").configure_limits(
    budget_usd=0.10,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,
)

runner = AgentRunner(
    model=llm,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    max_iterations=8,
)
runner.registry = ToolRegistry(
    runner.registry.tools,
    default_timeout_sec=30,
    circuit_breaker_config=CircuitBreakerConfig(failure_threshold=3),
)

session = Session.start(runner)
session.invoke("Read ./workspace/draft.md and rewrite it for clarity.")
session.save("./sessions/editing.json")

print(f"Spent: ${llm.usage.estimate_cost(0.003, 0.015):.4f}")
```

## Handle the "file doesn't exist" case

```python
result = runner.invoke(
    "If ./workspace/draft.md exists, improve it. Otherwise create it "
    "with a placeholder draft and note that you did so."
)
```

The `find_files` / `list_directory` tools let the model check first —
no special code needed. The model reasons via ReAct thoughts.

## Read-before-write safety

`write_file` refuses to overwrite an existing file the agent hasn't
read this session. If the model needs to overwrite anyway, it can pass
`if_exists='overwrite'` or `if_exists='rename'`.

## Add a custom formatter tool

Say you want to enforce a specific markdown style. Build a
`StructuredTool` that runs the file through a formatter:

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class FormatArgs(BaseModel):
    path: str = Field(..., description="Path to markdown file inside sandbox.")

def format_markdown(path: str) -> str:
    import subprocess
    result = subprocess.run(
        ["mdformat", path], capture_output=True, text=True,
    )
    return result.stdout or f"formatted {path}"

format_tool = StructuredTool(
    func=format_markdown,
    args_schema=FormatArgs,
    name="format_markdown",
    description="Run mdformat over a markdown file to enforce house style.",
)

runner = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[format_tool],
    permissions=Permissions.full_access(["./workspace"]),
)
```

DefaultTools + your custom tool run side-by-side. See [Custom tools](custom-tools.md).

## Stream the edit process live

```python
for event in runner.stream(
    "Improve ./workspace/draft.md, save to draft_v2.md, name 3 edits."
):
    if event["type"] == "thought":
        print("[thinking]", event["content"])
    elif event["type"] == "tool_call":
        print(f"[calling] {event['name']}({event['args']})")
    elif event["type"] == "tool_result":
        print(f"[done] {event['result'][:80]}")
    elif event["type"] == "final":
        print(f"\n{event['content']}")
```

## Common failure modes

| Symptom | Fix |
|---|---|
| Model says "file not found" but file exists | Check `allowed_paths` — the file must be inside a subtree the agent can reach. |
| Model writes to the wrong path | Set `workspace=` on Permissions; unqualified paths resolve there. |
| Model loops on `read_path` | The dup-guard will warn at 3, refuse at 5. If it keeps looping, lower `max_iterations` or narrow the task. |
| Cost creeps up | Add `configure_limits(budget_usd=...)` on the model. |
| The rewrite loses formatting | Add explicit instructions to preserve headers/code blocks, or add a `format_markdown` tool. |

## Full runnable example

See `examples/file_agent_demo.py` in the repo — same pattern, no
placeholders.
