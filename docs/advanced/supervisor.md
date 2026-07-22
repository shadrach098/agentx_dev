# Supervisor orchestration

The `Supervisor` pattern decomposes a task into a plan of sub-tasks and
dispatches each to a named specialist agent. Use when:


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

- The task naturally splits into pieces (research + writing +
  formatting).
- Different sub-tasks need different tool sets or permissions.
- You want one planner LLM call + N specialist executions + one
  synthesis LLM call.

Contrast: [Handoffs](handoffs.md) — dynamic mid-run routing between
peer agents.

## Minimum viable

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    Supervisor,
)

llm = Claude(model="claude-sonnet-4-6")   # or GPT() -- same API
file_agent = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, write_files=True, edit_files=True,
        list_directories=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
)

python_agent = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, write_files=True, execute_python=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
)

supervisor = Supervisor(
    model=llm,
    agents={
        "file_agent":   ("File management inside ./workspace", file_agent),
        "python_agent": ("Python code execution", python_agent),
    },
    max_subtasks=5,
    verbose=True,
)

result = supervisor.run(
    "Write a Python script that scrapes example.com for links, run it, "
    "and save the output to ./workspace/links.txt."
)
print(result.content)
```

## Flow

1. **Plan** — the supervisor's model receives the task + the specialist
   catalog and emits a JSON plan: `[{agent, description}, ...]`.
2. **Dispatch** — each sub-task goes to the named specialist. In
   sequential mode, findings from earlier steps are threaded into
   later sub-task queries as `PRIOR FINDINGS`.
3. **Synthesize** — the supervisor's model receives every specialist's
   final reply + the original task and emits the final answer.

The result is a `SupervisorResult`:

```python
result.content        # str  — final synthesized answer
result.subtasks       # List[SubtaskResult] — one per plan step
result.plan           # List[Dict] — the raw plan the planner emitted

for st in result.subtasks:
    print(f"[{st.agent}] {st.query[:60]}")
    print(f"  reply: {st.reply[:100]}")
```

## AsyncSupervisor

Same shape, sub-tasks run concurrently via `asyncio.gather`:

```python
from agentx_dev import AsyncSupervisor, AsyncAgentRunner

supervisor = AsyncSupervisor(
    model=llm,
    agents={...},
    sequential=False,   # concurrent (default). sequential=True threads findings.
    max_subtasks=5,
)
result = await supervisor.run("...")
```

Pass `sequential=True` when later sub-tasks need earlier findings.
Concurrent = no findings threading (each specialist runs independently).

## Dynamic specialist spawning

`SpawnConfig` lets the supervisor create NEW specialists mid-plan when
the initial catalog doesn't cover a capability:

```python
from agentx_dev import Supervisor, SpawnConfig

supervisor = Supervisor(
    model=llm,
    agents={
        "file_agent": ("File management", file_agent),
    },
    spawn_config=SpawnConfig(
        enabled=True,
        auto_spawn=True,      # False = prompt on stdin (or use approver=)
        allowed_paths=["./workspace"],
        max_spawns=3,         # runaway guard
        auto_spawn_allowed_caps={"web"},   # 3.0.6: allowlist for auto-spawn
    ),
)
```

**Recognized capability keywords the planner can request:**

- `"web"` — installs `web_search` + `web_fetch`.
- `"files"` — read / write / edit / list inside the sandbox.
- `"code"` — `run_python`.
- `"delete"` — adds delete permission on top of `files`.

**Capability-overlap guard:** the framework refuses duplicate spawns.
If you already registered a specialist with those tools and the planner
tries to spawn another, the spawn is refused AND follow-up dispatches
to the refused name auto-reroute to the existing specialist.

**`auto_spawn_allowed_caps` (3.0.6)** — a security allowlist. When
`auto_spawn=True`, only these capabilities silently spawn. A
prompt-injected task can't talk the planner into a `["code"]` or
`["delete"]` spawn if those aren't in the allowlist. Recommended:
`{"web"}` for a safe default.

## Approval callbacks

For human-in-the-loop spawn approval instead of `auto_spawn`:

```python
def approve(request):
    print(f"Planner wants to spawn: {request.name} with caps {request.capabilities}")
    return input("y/n? ") == "y"

spawn_config = SpawnConfig(
    enabled=True, auto_spawn=False, approver=approve,
    allowed_paths=["./workspace"],
)
```

`request` is a `SpawnRequest` with `name`, `description`,
`capabilities`, `rationale`.

## Verbose mode

`verbose=True` on the supervisor prints:

- The plan the planner emitted.
- Each dispatch (`[dispatch] file_agent: <query>`).
- Each specialist's final reply.
- The synthesized final answer.

Great for debugging your specialist catalog + descriptions.

## Findings threading

In sequential mode, the framework auto-threads earlier sub-task replies
into later queries as a `PRIOR FINDINGS` block. Specialists see what
their predecessors produced without you writing glue code:

```
[researcher] find 3 competitors
    -> reply: Company X, Company Y, Company Z

[writer] draft an email introducing us to the competitors
    receives:
      PRIOR FINDINGS:
      [researcher] Company X, Company Y, Company Z

      TASK: draft an email introducing us to the competitors
```

## When NOT to use Supervisor

- **Single-tool tasks** — use `AgentRunner` directly.
- **Dynamic routing based on partial results** — use [handoffs](handoffs.md).
- **Streaming intermediate progress to a UI** — Supervisor doesn't
  stream; use handoffs + `runner.stream()` for that.
- **Deeply nested planning** — Supervisor is one level deep. For
  hierarchical decomposition, run supervisors within supervisor sub-tasks.

## Rules (from AGENTX.md §7)

- **Minimum viable plan** — every step is a full LLM call. Merge
  adjacent steps that would go to the same specialist. If the task
  fits ONE specialist's scope, use ONE step.
- **Sub-agents run in isolation** — they do NOT see previous steps'
  output unless the framework threads it (sequential mode) or you
  paste findings manually.
- **No final "report" step** — the framework's synthesis stage already
  lifts sub-task results into the final answer. A sub-task with query
  "summarize what was found" is wasted.
- **Anti-fabrication in synthesis** — the supervisor's synthesis pass
  uses ONLY facts explicitly present in specialist replies. If a
  specialist only sent "task complete," the synthesis says so
  plainly — never reconstructs what the file "probably" contains.

## Runnable examples

- `examples/orchestration_demo.py` — Single agent / orchestrator-as-ReAct
  / Supervisor with dynamic spawn side-by-side.
- `examples/supervisor_codebase_analysis_demo.py` — Supervisor
  analyzing its own source tree. Default tools only. Exercises
  dynamic-spawn machinery and persistent-state.
