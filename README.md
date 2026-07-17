# agentx_dev

A small, production-ready Python framework for building LLM agents.
Think LangChain, minus the sprawl.

This README walks you through real usage — start an agent, add tools,
connect MCP servers, ship to production. Every code block is something
you can paste and run.

---

## What's new in 3.0

- **Multi-agent orchestration** — `Supervisor` decomposes a task into
  a plan of sub-tasks, dispatches each to the right specialist, and
  synthesizes the results into one answer. Sync and async variants.
- **Dynamic specialist spawning** — `SpawnConfig` lets a Supervisor
  spin up NEW specialists mid-plan when the initial catalog doesn't
  cover a capability, with a capability-overlap guard that refuses
  duplicate spawns and auto-reroutes follow-up dispatches.
- **New default tools** — `grep`, `find_files`, `run_shell` join the
  existing file / code bundle. All noise-dir-skipping, sandbox-checked,
  cross-platform.
- **Web tools** — `web_search`, `web_fetch` (DuckDuckGo + Wikipedia
  fallback, no API keys). Optional disk-cache mode writes full page
  bodies to a file so `run_python` can `open()` them instead of
  re-transmitting HTML.
- **Persistent Python state** — enable
  `Permissions(python_persistent_state=True)` and every `run_python`
  call gets a `state` dict auto-loaded and saved between calls.
  Jupyter-like workflow inside subprocess isolation.
- **Session directories** — `mint_session_dir()` /
  `Permissions.new_session()` give every run its own subdirectory,
  so 25 parallel runs produce 25 non-clobbering outputs.
- **Anti-spiral guards** — the framework catches duplicate tool calls
  (warn at 2, refuse at 5) and force-terminates ReAct loops that
  emit 3 identical `(action, args)` in a row. Models that would have
  burned max_iterations now bail cleanly with a real result.
- **Project guide auto-discovery** — drop an `AGENTX.md` at your repo
  root and the framework auto-points every specialist at it via the
  system prompt. See §"Project guide" below.
- **Prompt template rewrite** — every built-in `AgentType` template
  (ReAct / Instruction-Tuned / Chain-of-Thought / Zero-Shot / Few-Shot)
  now follows a unified shape: HARD RULES → PROCESS checklist →
  ANTI-PATTERNS → OUTPUT CONTRACT → worked examples.

Upgrading from 2.x? Almost everything is additive. The only signature
break is internal to `Supervisor._handle_spawn` (now returns a tuple
for the auto-reroute feature); external `Supervisor.run()` /
`AsyncSupervisor.run()` are unchanged.

---

## Install

Base install (OpenAI provider + all core features):

```bash
pip install agentx-dev
```

Add optional providers / integrations as extras:

```bash
pip install agentx-dev[anthropic]      # enables agentx_dev.Claude
pip install agentx-dev[mcp]            # enables MCPClient (stdio / sse / http)
pip install agentx-dev[otel]           # OpenTelemetry observability adapter
pip install agentx-dev[all]            # everything above
```

Requires Python **3.10+** (the codebase uses PEP 604 union types).

Set your provider API key as an environment variable:

```bash
export OPENAI_API_KEY=sk-...             # macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...      # if you installed [anthropic]

$env:OPENAI_API_KEY = "sk-..."           # Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

---

## Your first agent — 5 lines

```python
from agentx_dev import AgentRunner, AgentType, Claude

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])
result = runner.invoke("What is 12 * 47? Reason step by step.")
print(result.content)
```

That's an agent. No tools yet — the model just thinks and answers. Let's
give it something to do.

---

## Add a custom tool

A tool is a Python function the agent can call. Two flavors:

### Simple tool — one string argument

```python
from agentx_dev import AgentRunner, AgentType, Claude, StandardTool

def get_weather(city: str) -> str:
    """Look up the current weather for a city."""
    # In a real app you'd call a weather API here.
    return f"It's 72°F and sunny in {city}."

weather_tool = StandardTool(
    func=get_weather,
    name="get_weather",
    description="Get the current weather for a city.",
)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[weather_tool],
)
result = runner.invoke("What's the weather in San Francisco?")
print(result.content)
# → "It's 72°F and sunny in San Francisco."
```

### Structured tool — typed arguments

When your tool needs multiple arguments, define them with a Pydantic
schema so the LLM gets the field names and types right:

```python
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, Claude, StructuredTool

class CalcArgs(BaseModel):
    a: float
    b: float
    op: str   # "add" / "sub" / "mul" / "div"

def calculator(a: float, b: float, op: str) -> str:
    """Do basic arithmetic."""
    ops = {"add": a + b, "sub": a - b, "mul": a * b, "div": a / b}
    return f"{a} {op} {b} = {ops[op]}"

calc_tool = StructuredTool(
    func=calculator,
    args_schema=CalcArgs,
    name="calculator",
    description="Perform basic arithmetic (add/sub/mul/div).",
)

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[calc_tool])
result = runner.invoke("What is 47 multiplied by 12?")
print(result.content)
# → "47 mul 12 = 564.0"
```

The LLM sees the schema, fills in the right fields, and the framework
validates the arguments before your function runs.

---

## Default tools (file access + Python execution)

Most agents need to read files, write files, and run code. Instead of
hand-rolling those (with sandbox checks, timeouts, etc.), the framework
ships them under a permission system.

### Auto-create a config file on first run

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.from_file(),
)
```

First time you run this, it creates `.agentx/permissions.json` in your
project with **deny-all** defaults:

```json
{
  "_comment": "Edit this file to control which capabilities the agent has...",
  "_docs": "https://github.com/shadrach098/Bruce_framework",
  "read_files": false,
  "list_directories": false,
  "write_files": false,
  "edit_files": false,
  "delete_files": false,
  "move_files": false,
  "execute_python": false,
  "allowed_paths": null,
  "python_timeout_sec": 10.0,
  "python_max_output_bytes": 100000,
  "max_file_size_bytes": 10485760
}
```

Open the file, grant the capabilities you want, save:

```json
{
  "read_files": true,
  "list_directories": true,
  "write_files": true,
  "execute_python": true,
  "allowed_paths": ["./workspace"]
}
```

Re-run — the agent now has access to those tools. **No code change.**

### Or configure permissions inline

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True,
        list_directories=True,
        write_files=True,
        allowed_paths=["./workspace"],   # sandbox
        python_timeout_sec=10,
    ),
)
result = runner.invoke(
    "Create a file at ./workspace/hello.txt with the text 'hi from the agent'"
)
print(result.content)
```

### Permission presets

For common patterns:

```python
Permissions.deny_all()                              # the default
Permissions.read_only(allowed_paths=["./docs"])     # read + list only
Permissions.full_access(["./project"])                # full access in sandbox
```

### What tools you get

Once granted, the agent sees these tools:

| Tool | What it does | Capability |
|---|---|---|
| `read_path` | Read a file (line-numbered slice via `offset`/`limit`) | `read_files` |
| `list_directory` | List a folder | `list_directories` |
| `find_files` | Search by glob (auto-skips `.git`, `node_modules`, `__pycache__`, `.venv`, etc.) | `list_directories` |
| `grep` | Content search — returns `file:line: match`, cross-platform, regex or literal | `read_files` |
| `write_file` | Create / update a file. `if_exists=` controls behavior on collision (`refuse` / `rename` / `overwrite` / `append`) | `write_files` |
| `edit_file` | Replace a substring in a file (exactly one match required) | `edit_files` |
| `delete_path` | Delete a file or directory (`recursive=True` for non-empty dirs) | `delete_files` |
| `move_file` | Move / rename | `move_files` |
| `run_python` | Execute a Python snippet in a fresh subprocess | `execute_python` |
| `run_shell` | Execute a one-shot shell command (bash on Unix, git-bash / PowerShell on Windows) | `execute_shell` |

### Security guarantees

- `allowed_paths` is enforced. `../../etc/passwd` is blocked — the path
  resolver flattens `../` before the subtree check.
- `run_python` runs in a fresh subprocess with a wall-clock timeout and
  an output-byte cap. A runaway script can't lock your agent.
- Denied capabilities → those tools aren't even registered. The agent
  never sees them as options.

### Combine defaults with your own tools

```python
runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[weather_tool, calc_tool],              # your custom tools
    permissions=Permissions.full_access(["./work"]),  # adds 7 default tools
)
result = runner.invoke(
    "Look up the weather in Paris, then write it to ./work/paris.txt"
)
```

### Persistent state in run_python

Turn on `python_persistent_state=True` and every `run_python` call gets
a `state` dict auto-loaded from `<workspace>/.run_python_state.pkl`.
Model can pass data between calls without re-defining setup:

```python
perms = Permissions(
    read_files=True, write_files=True, execute_python=True,
    allowed_paths=["./workspace"], workspace="./workspace",
    python_persistent_state=True,   # ← enables state dict
)

# Turn 1: setup
# state['files'] = [f for f in Path('./src').rglob('*.py')]

# Turn 2: analysis, no redefinition needed
# print(sum(len(open(f).read().splitlines()) for f in state['files']))
```

Only picklable objects survive round trips. Non-picklable values
(lambdas, threads, live handles) are silently dropped with a stderr
note listing the keys. Clear state with `state.clear()` or delete the
hidden `.run_python_state.pkl`.

### Per-run session directories

Running the same demo multiple times? Batch jobs? Parallel evaluations?
`mint_session_dir` and `Permissions.new_session` isolate each run so
outputs don't clobber each other:

```python
from agentx_dev import mint_session_dir, Permissions

# Mint a fresh subdirectory: ./workspace/run_<UTC timestamp>_<8-char id>/
session_dir = mint_session_dir("./workspace", prefix="run_")

perms = Permissions(
    write_files=True, execute_python=True,
    allowed_paths=[session_dir],
    workspace=session_dir,
)
# Or in one call:
perms = Permissions.new_session(base="./workspace")
```

Every run mints a sibling subdirectory. 25 runs → 25 distinct output
folders. Sortable by timestamp, uniquely suffixed by a random id.

### Web search + fetch (optional)

Not part of DefaultTools — plug them in explicitly when you want the
agent to read the internet:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    web_search_tool, web_fetch_tool,
)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    tools=[
        web_search_tool(),
        # Optional: cache_dir=... saves the FULL response body to disk,
        # and the tool response includes a "open(cached_path).read()"
        # snippet the model can drop straight into run_python — avoids
        # re-transmitting HTML through the LLM.
        web_fetch_tool(cache_dir="./workspace"),
    ],
)
```

DuckDuckGo with Wikipedia fallback. No API keys, no extra dependencies.

---

## Multi-agent orchestration (Supervisor)

For tasks that decompose into distinct sub-problems, register several
specialists and let a `Supervisor` decide which one handles each step:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions
from agentx_dev.Supervisor import Supervisor

llm = Claude(model="claude-sonnet-4-6")

file_agent = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, write_files=True, edit_files=True,
        list_directories=True, delete_files=True,
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
    verbose=True,   # framework prints plan / dispatch / result / final
)

result = supervisor.run(
    "Write a Python script that scrapes example.com for links, run it, "
    "and save the output to ./workspace/links.txt."
)
print(result.content)
```

Flow: ONE LLM call decomposes the task into a plan → each sub-task
dispatches to the named specialist → ONE LLM call synthesizes the
sub-task results into a final answer.

Findings from earlier steps get automatically threaded into later
steps' queries, so a plan like `researcher → writer` sees the
researcher's actual output in the writer's prompt (no manual passing
required).

### Dynamic specialist spawning

`SpawnConfig` lets the Supervisor create NEW specialists mid-plan when
the initial catalog doesn't cover a capability. Great for tasks whose
shape isn't known upfront:

```python
from agentx_dev.Supervisor import Supervisor, SpawnConfig

supervisor = Supervisor(
    model=llm,
    agents={
        # Start with a lean catalog — Supervisor will spawn what it needs.
        "file_agent": ("File management inside ./workspace", file_agent),
    },
    spawn_config=SpawnConfig(
        enabled=True,
        auto_spawn=True,      # False = prompt on stdin (or use approver=)
        allowed_paths=["./workspace"],
        max_spawns=3,         # runaway guard
    ),
)
```

Recognized capability keywords the planner can request:
- `"web"` — installs `web_search` + `web_fetch`.
- `"files"` — read / write / edit / list inside the sandbox.
- `"code"` — `run_python`.
- `"delete"` — adds delete permission on top of `files`.

The framework's capability-overlap guard refuses duplicate spawns
(you already registered a specialist with those tools? planner tries
to spawn another one? refused, and any follow-up dispatches to the
refused name auto-reroute to the existing specialist).

### AsyncSupervisor

Same shape, but sub-tasks run concurrently via `asyncio.gather`:

```python
from agentx_dev.Supervisor import AsyncSupervisor

supervisor = AsyncSupervisor(
    model=llm,
    agents={...},
    sequential=False,   # concurrent (default). sequential=True to thread findings.
)
result = await supervisor.run("...")
```

Pass `sequential=True` if your sub-tasks depend on earlier steps'
output (concurrent execution means no findings-threading between
steps; sequential preserves ordering AND passes findings forward).

---

## Project guide (AGENTX.md)

Drop a file named `AGENTX.md` at your repo root or workspace. Every
AgentRunner auto-discovers it and points the model at it in the
system prompt:

> PROJECT GUIDE: an AGENTX.md file exists at &lt;path&gt;. READ IT FIRST
> via read_path — it documents the conventions this codebase expects
> (tool patterns, sandbox layout, common failure modes, when to spawn
> specialists, how to author custom tools).

Think of it as `CLAUDE.md`-style: instructions written for the model
that lands in your repo, not for humans reading the README. The
framework doesn't inline the file's contents (that would burn tokens
on every call) — it just points at it. Model reads on demand.

The `AGENTX.md` shipped with this repo is a template you can adapt
(or delete + write your own). See the repo root.

---

## Connect an MCP server

MCP (Model Context Protocol) lets you plug in tool servers without
writing tool code yourself. Want filesystem access via the official
filesystem server? Want database queries via a Postgres MCP server?
Just connect.

### Stdio (local subprocess — most common)

```python
import asyncio
from agentx_dev import AsyncAgentRunner, AgentType, Claude, MCPClient

async def main():
    # Spawn the official filesystem MCP server
    async with MCPClient.connect_stdio(
        "npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"
    ) as mcp:
        tools = await mcp.list_tools()   # auto-discovers what the server offers

        runner = AsyncAgentRunner(
            model=Claude(),
            agent=AgentType.ReAct,
            tools=tools,
        )
        result = await runner.ainvoke("List the files in /path/to/dir")
        print(result.content)

asyncio.run(main())
```

### HTTP / SSE (remote MCP server)

```python
mcp = await MCPClient.connect_http(
    "https://my-mcp-server.example.com/mcp",
    headers={"Authorization": "Bearer your-token"},
)

mcp = await MCPClient.connect_sse(
    "https://my-mcp-server.example.com/sse",
    headers={"Authorization": "Bearer your-token"},
)
```

### MCP resources + prompts

Beyond tools, MCP servers can expose readable resources and pre-baked
prompt templates:

```python
async with MCPClient.connect_stdio(...) as mcp:
    # List + read resources
    resources = await mcp.list_resources()
    content = await mcp.read_resource("file:///readme.md")

    # Wrap a resource as a tool the agent fetches on demand
    readme_tool = mcp.resource_as_tool("file:///readme.md")

    # Pre-baked prompts
    prompts = await mcp.list_prompts()
    messages = await mcp.get_prompt("summarize", {"text": "..."})
```

---

## Persist conversations across restarts

Without this, every restart loses state. With `Session`, your agent
picks up exactly where it left off:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions, Session

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./work"]),
)

# First conversation
session = Session.start(runner, metadata={"user_id": "alice"})
session.invoke("Plan a 3-day trip to Paris.")
session.save("./sessions/paris-trip.json")

# ... your program exits ...

# Resume tomorrow — reattach the runner, continue
session = Session.load("./sessions/paris-trip.json", runner=runner)
session.invoke("Add a visit to Versailles on day 2.")
session.save("./sessions/paris-trip.json")
```

What's saved: full history (with provider-native tool_use IDs),
cumulative tool calls, token usage, metadata. The atomic write
guarantees a crashed save can't corrupt an existing valid file.

---

## Get a typed result back

Force the agent's final answer to fit a Pydantic schema:

```python
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, Claude

class Receipt(BaseModel):
    merchant: str
    total: float
    currency: str = "USD"

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])
result = runner.invoke(
    "Parse this receipt into structured fields: 'Joe's Diner, $12.50 USD'",
    output_schema=Receipt,
)

print(result.output)            # Receipt(merchant="Joe's Diner", total=12.5, currency='USD')
print(result.output.merchant)   # "Joe's Diner"
print(result.output.total)      # 12.5
```

Or skip the agent loop entirely for one-shot extraction:

```python
extractor = Claude().with_structured_output(Receipt)
receipt = extractor.invoke("Joe's Diner, $12.50")
print(receipt.merchant)
```

---

## Stream output token-by-token

```python
runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[weather_tool])

for event in runner.stream("What's the weather in NYC?", stream_tokens=True):
    if event["type"] == "text_delta":
        print(event["content"], end="", flush=True)
    elif event["type"] == "tool_call":
        print(f"\n→ calling {event['name']}")
    elif event["type"] == "tool_result":
        print(f"\n✓ {event['result']}")
    elif event["type"] == "final":
        print(f"\n\n💬 {event['content']}")
```

---

## Production controls

Set them on the model once and forget. Everything's opt-in.

```python
from agentx_dev import Claude

llm = Claude(model="claude-sonnet-4-6").configure_limits(
    # Don't spend more than $5
    budget_usd=5.0,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,

    # Don't exceed 5 LLM calls per second
    rate_limit_per_sec=5,

    # Don't retry more than 10 times total over this model's lifetime
    retry_budget=10,
)
```

Crossing the budget raises `CostBudgetExceeded` immediately. The agent
loop stops, no more API calls.

```python
# Per-tool timeouts + circuit breakers
from agentx_dev import AgentRunner, AgentType, ToolRegistry, CircuitBreakerConfig

runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[weather_tool])
runner.registry = ToolRegistry(
    runner.registry.tools,
    default_timeout_sec=30,    # no tool runs longer than 30s
    circuit_breaker_config=CircuitBreakerConfig(
        failure_threshold=5,    # 5 fails in a row → trip
        recovery_timeout_sec=60, # stay open for 1 minute
    ),
)
```

Track usage + estimate cost any time:

```python
runner.invoke("...")
runner.invoke("...")
print(llm.usage)
# TokenUsage(calls=2, input=3210, output=587, total=3797)

print(f"spent ${llm.usage.estimate_cost(0.003, 0.015):.4f}")
# spent $0.0184
```

---

## Configure an agent from YAML

When you want to swap models / tools / permissions without code changes:

```yaml
# agent.yaml
model:
  provider: claude          # gpt | claude | custom
  name: claude-sonnet-4-6
  temperature: 0.7

agent_type: ReAct           # ReAct | Chain_of_Thought | Zero_Shot | Few_Shot | Instruction_Tuned

permissions:
  read_files: true
  list_directories: true
  write_files: true
  allowed_paths:
    - ./workspace

tools:
  - module: my_app.tools
    attr: weather_tool

max_iterations: 6
use_function_calling: true
verbose: false
```

Load it:

```python
from agentx_dev import load_agent_from_yaml

runner = load_agent_from_yaml("agent.yaml")
result = runner.invoke("What's in the workspace?")
```

A typo in any field (`read_filez: true`) fails loudly so you know
exactly which line to fix.

---

## End-to-end example: file-editing agent

Putting it together — an agent that reads a file, asks the LLM to
improve it, writes the improved version, and reports what changed:

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
    "Read ./workspace/draft.md. Improve the writing for clarity and brevity. "
    "Save the improved version to ./workspace/draft_v2.md. "
    "Tell me three specific edits you made."
)

print(result.content)
for tc in result.tool_calls:
    print(f"  {tc.name}({tc.args})")
```

Cost-control variant — same agent, capped at $0.10 with a circuit
breaker, persistable session:

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

---

## Where to go next

- More runnable demos: see [`examples/`](examples/)
  - `file_agent_demo.py` — agent that reads / writes / runs code
  - `function_calling_demo.py` — structured output extraction
  - `mcp_demo.py` — end-to-end MCP server integration
  - `orchestration_demo.py` — the three orchestration patterns side-by-side
    (single agent / orchestrator-as-ReAct / Supervisor with dynamic spawn)
  - `supervisor_codebase_analysis_demo.py` — Supervisor-only, default
    tools only. Analyzes its own source tree to produce a Markdown report,
    exercising the dynamic-spawn machinery and the persistent-state feature.
- Project-scoped instruction sheet: [`AGENTX.md`](AGENTX.md) —
  read for the conventions this codebase expects.
- The full framework docs: see [`agentx_dev/README.md`](agentx_dev/README.md)
  (every class, every method, every operational lever)

## Links

- Repo: https://github.com/shadrach098/Bruce_framework
- Issues: https://github.com/shadrach098/Bruce_framework/issues
