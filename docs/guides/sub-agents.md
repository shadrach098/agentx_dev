# Sub-agents

A Supervisor can create its own helpers, the way Claude Code launches a sub-agent for a side job. There are two ways, and both go through one set of limits that you set once.

- **The planner defines a helper inline.** When no specialist you registered fits a sub-task, the planner writes a new one into the plan: a name, its own instructions, and the tools it needs. The Supervisor builds it, runs the step on it, and discards it when the run ends.
- **A specialist hands off part of its own work.** While working, a specialist can call a `delegate` tool. A fresh agent does the side job in a clean context and a short summary comes back, so the specialist's own context stays small.

Nothing is on by default, except in persistent mode (below).

## Quick start

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Supervisor, SpawnConfig

model = GPT(model="gpt-4o-mini")     # or Claude(...); any chat model works

# A specialist you register yourself: it can read and list files under ./workspace.
explorer = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    tools=[],
    permissions=Permissions(
        read_files=True, list_directories=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
    system_addendum="You read files in the workspace and report what is in them. You cannot browse the web.",
    verbose=False,
)

supervisor = Supervisor(
    model=model,
    agents={"explorer": ("Reads and lists files in ./workspace", explorer)},   # your specialists
    spawn_config=SpawnConfig(
        enabled=True,
        capabilities={"web", "files_read"},   # the ceiling: what a spawned agent may use
        allowed_paths=["./workspace"],        # its file sandbox
        max_spawns=6,                         # sub-agents per run, plans and delegations together
    ),
)

result = supervisor.run("Compare the pricing pages of our three competitors")
for sub in result.spawned:
    print(sub["name"], sub["origin"], sub["tools"], sub["outcome"])
```

`AsyncSupervisor` takes the same `spawn_config=`. Spawned steps run in the same parallel batches as every other step.

A runnable version of this is in the repo: `python examples/subagents_demo.py` (it creates `./workspace/competitors.md` on the first run, accepts your own task as an argument, and takes `--persistent` to add recovery rounds and a shared deadline).

## What the planner writes

A plan step can carry its own agent instead of naming one:

```json
{"id": "s2", "query": "Compare the three pricing pages",
 "new_agent": {"name": "pricing_analyst",
               "instructions": "You compare SaaS pricing. Return a table: plan, price, limits, source URL.",
               "tools": ["web_search", "web_fetch"]}}
```

- `instructions` are free-form: the planner tells the helper who it is and what to return. The framework always adds its own rule: return the real data and never invent it.
- `tools` name tools the ceiling allows. Anything else is dropped and the planner is told what was dropped.
- Later steps reuse the helper by name (`{"agent": "pricing_analyst", ...}`) without defining it again. Repeating an identical definition also reuses it for free. A different definition under the same name gets a suffix (`pricing_analyst_2`).
- A name that matches a specialist you registered runs that specialist and ignores the definition.
- Names starting with `__` and names like `delegate_1` are reserved; a definition that uses one is dropped and the planner is asked to fix the plan.
- A helper exists for one run. When the run ends, your supervisor's registry is exactly as it was.

## The ceiling

The planner's plan is model output, which can be steered by the user's text. So you decide the most a helper may ever do, once, and nothing the planner or a helper writes can raise it.

| `SpawnConfig(...)` | Default | What it does |
|---|---|---|
| `enabled` | `False` | Master switch. |
| `capabilities` | `None` | Preset words a helper may use: `web` (search and fetch), `files_read` (read, list, find, grep), `files` (also write and edit), `code` (`run_python`), `delete`. Setting this or `tools` turns on ceiling mode. |
| `tools` | `None` | A pool of your own tool objects a helper may pick from by name. |
| `allowed_paths` | `["./workspace"]` | The file sandbox for `files_read`, `files`, `code` and `delete`. |
| `max_spawns` | 3 (6 in ceiling mode) | Sub-agents per run, planner spawns and delegations together. |
| `max_depth` | `1` | A helper can't spawn its own helpers. Raise it to allow deeper trees. |
| `approver` | `None` | Optional `(SpawnRequest) -> bool`. In ceiling mode it is an extra gate, not required. |
| `auto_spawn`, `auto_spawn_allowed_caps` | | The older approval flow, used only when neither `capabilities` nor `tools` is set. |

Inside the ceiling, nobody is asked for approval, so a long unattended run never stalls. A request outside it is clipped, not fatal: the helper is built with what is allowed. If a helper asked for tools and none are allowed, the step fails with `spawn refused: no usable tools`.

Your own tools join the pool like this:

```python
from agentx_dev import SpawnConfig, StandardTool

def lookup(query: str) -> str:
    return f"results for {query}"

lookup_tool = StandardTool(func=lookup, name="lookup", description="Search our internal wiki")
config = SpawnConfig(enabled=True, tools=[lookup_tool], capabilities={"web"})
```

## Delegation

Every specialist of a Supervisor with a ceiling set (`capabilities=` or `tools=`, or the persistent default below) gets a `delegate` tool for the duration of the run (it is removed afterwards, so your runners are untouched). A legacy config with neither (the 3.5 `auto_spawn`/`approver` flow) gives specialists no `delegate`; the planner can still define helpers, through that approval flow.

```
delegate(task, instructions="", tools=[])  ->  the sub-agent's answer
```

- The helper sees only `task`, not the specialist's conversation. A thin `task` gives a thin result, so the specialist is told to put every needed fact in it.
- The answer comes back as the tool result, cut at 4,000 characters with a `[truncated]` marker. Only the outcome and size are kept on the run record (`result.spawned`); the answer is returned to the specialist, so it appears in that specialist's own output.
- When the ceiling clipped some of the requested tools, the tool result ends with `[note: these tools were not granted: ...]`, so the specialist knows what the helper could not use.
- A refusal comes back as plain text (`delegation refused: spawn limit reached; do this yourself`). It is information, not an error.
- A helper that gives up or crashes comes back as a tool error that starts `[delegate failed: stuck]` (or `error`, `out_of_time`, ...). The specialist's own stuck logic sees it and can retry with different instructions or do the work itself. A failed delegation never ends the specialist's run.

You can also give a standalone runner the same ability:

```python
from agentx_dev import AgentRunner, AgentType, SpawnConfig

researcher = AgentRunner(
    model=model, agent=AgentType.ReAct, tools=my_tools,
    delegation=SpawnConfig(enabled=True, capabilities={"web"}),
)
result = researcher.invoke("Survey the market. Hand each competitor to a helper.")
print(researcher.spawned)      # name, origin, tools, dropped, outcome, chars for this run
```

In the async runner, several `delegate` calls in one model turn run at the same time when the runner dispatches a turn's tool calls together (`bind_tools_natively=True`).

## Persistent mode

With `persistence=Persistence(...)` on a Supervisor and no `spawn_config`, spawning is on with a safe ceiling: `capabilities={"web", "files_read"}`, `max_spawns=6`. Pass `spawn_config=SpawnConfig(enabled=False)` to turn it off, or your own config to change it.

- Helpers inherit `persistence`: they get the same stuck handling, ledger and compaction, and they share the one run deadline and cost cap.
- A recovery plan can define a replacement helper and mark the failed step as replaced (`"replaces": ["s1"]`), so a stuck specialist can be swapped for a differently-instructed one.
- Code execution, writes and deletes are not in the default ceiling. Allow them explicitly in your own `SpawnConfig`.

See [Long-running agents](long-running-agents.md).

## Watching it

`supervisor.stream(task)` includes:

| Event | Meaning |
|---|---|
| `{"type": "spawn", "name", "origin": "plan"\|"delegate", "tools", "dropped", "reused", "refused", ...}` | A helper was built, reused, or refused. |
| `{"type": "delegate_result", "name", "outcome", "chars"}` | A delegation returned. |

With `verbose=True` the same moments print as `[spawn]` lines.

## Things to know

- **The ceiling bounds tools, not behavior.** Instructions can steer a helper within what you allowed (for example to fetch a particular URL with `web`). Keep `code`, `files` and `delete` out of the ceiling for supervisors that handle untrusted input.
- **Cost.** Each helper is a full agent with its own turns. `max_spawns` and `max_depth` bound the count; for unattended runs also use `Persistence` or a model cost cap (`model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)`).
- **Fresh context is the point, and the catch.** A helper knows nothing you don't put in its task.
- **Tool cache.** Helpers in persistent mode run with the tool-result cache off, like any persistent agent.
- **One run at a time per instance.** A Supervisor or AsyncSupervisor keeps its run's state (the helper registry and spawn count) on the instance, so don't run two tasks on one instance at the same time; use one instance per concurrent run. The same goes for a runner built with `delegation=`.
- **The ceiling, not the parent's permissions.** A specialist's `delegate` grants tools from the `SpawnConfig` ceiling, regardless of the specialist's own `permissions=`.
- **Helpers a planner defines can't delegate** at the default `max_depth=1`; only the specialists you registered can. Raise `max_depth` to allow it.
