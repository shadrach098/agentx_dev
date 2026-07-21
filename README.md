# agentx_dev

A production-grade Python framework for building LLM agents.
Nothing sprawls.

This README walks you through real usage — start an agent, add tools,
connect MCP servers, ship to production. Every code block is something
you can paste and run.

---

## What's new in 3.1 — power features

Six force-multipliers land in one release. All additive; existing code
keeps working.

| Feature | What you get |
|---|---|
| **Anthropic prompt caching** | `Claude(enable_prompt_cache=True)` marks the system prompt, tool schemas, and (for long chats) stable history segments with `cache_control: ephemeral`. Repeat calls that share those blocks read them from Anthropic's cache -- typically ~90% cheaper on cached input. `TokenUsage` now surfaces `cache_hit_ratio` so you can verify hits. |
| **Parallel tool calls per turn (sync)** | `AgentRunner(bind_tools_natively=True)` binds your tools as native FC tools. When the LLM emits multiple `tool_use` blocks in one turn, they dispatch concurrently on a `ThreadPoolExecutor` (`parallel_tool_workers=8` by default). Async runner already had this; sync now matches. |
| **Semantic long-term memory** | `SemanticMemory(embeddings=...)` embeds every message and retrieves the top-K most relevant older turns for the current query -- keeps recent tail verbatim, injects retrieved older context as a synthetic system message. Beats sliding-window on multi-topic conversations. |
| **`vector_search` RAG tool** | `vector_search_tool(store)` gives your agent a native RAG tool. Backed by `VectorStore` -- in-memory cosine (numpy-accelerated when available), save/load to JSON. `HashEmbeddings` runs offline for dev + tests; `OpenAIEmbeddings` for production. |
| **Agent-to-agent handoffs** | `handoff_tool("researcher")` + `HandoffCoordinator({...}, entry="triage")` let one agent transfer control to a peer mid-run (Swarm-style). Bounded by `max_hops`; conversation history propagates so the target agent starts fully informed. |
| **Evals harness** | `EvalCase` + `EvalRunner` run declarative test cases against any runner, collect per-case latency + tokens + tool-error counts, and produce a pass/fail report. `contains`, `called_tool`, `matches_regex`, `llm_judge`, and more assertion helpers included. CLI: `python -m agentx_dev.Evals run tests/evals/ --config agent.yaml`. |

### 3.1 API reference — everything new at a glance

Grouped by module. All symbols below are exported at the package top level
(`from agentx_dev import X`) unless marked `agentx_dev.X.Y`.

**1. Prompt caching (`agentx_dev.ChatModel`)**

New parameters on `Claude.__init__`:

| Param | Type | Default | What it does |
|---|---|---|---|
| `enable_prompt_cache` | `bool` | `False` | Turn on Anthropic prompt caching. System prompt + tool schemas get `cache_control: ephemeral`. Second call reads them from cache. |
| `cache_history_after` | `int` | `4` | When history has more than N turns, add a third cache breakpoint on the last stable assistant message so long chats also benefit. Max 3 breakpoints used (Anthropic allows 4). |

New `TokenUsage` attributes + property:

| Attribute | What it tracks |
|---|---|
| `total_cache_read_tokens` | Cumulative tokens served from cache (discounted rate). |
| `total_cache_creation_tokens` | Cumulative tokens written into cache (small premium). |
| `last_cache_read_tokens` | Cache read tokens from the most recent call. |
| `last_cache_creation_tokens` | Cache creation tokens from the most recent call. |
| `cache_hit_ratio` *(property)* | `total_cache_read_tokens / total_input_tokens`. Zero when no cache. |

`TokenUsage.record()` gained two keyword args: `cache_read_tokens`, `cache_creation_tokens` (default `0` — providers that don't cache don't need to pass them).

**2. Parallel per-turn tool dispatch (`agentx_dev.Runner.AgentRun`)**

New parameters on `AgentRunner.__init__`:

| Param | Type | Default | What it does |
|---|---|---|---|
| `bind_tools_natively` | `bool` | `False` | Skip the AgentType parser indirection; bind your tools directly as native FC tools so the LLM can call them (and multiple at once per turn). Mutually exclusive with `use_function_calling`. Mirrors the async runner's mode of the same name. |
| `parallel_tool_workers` | `int` | `8` | Max threads in the pool that runs concurrent tool calls when native binding is on. Bounded by `min(workers, num_calls)` per turn. |

Auto-registered when `bind_tools_natively=True`: a synthetic `respond` tool the LLM calls to end the loop with its final answer.

**3. Embeddings + vector store + RAG (`agentx_dev.Embeddings`)**

New module. Exports:

| Symbol | Kind | What it is |
|---|---|---|
| `Embeddings` | ABC | Base class. `embed(texts) -> list[list[float]]`, `embed_one(text)`, `dim: int`. |
| `OpenAIEmbeddings(model="text-embedding-3-small", api_key=None, base_url=None, batch_size=100)` | class | OpenAI-backed. Auto-batches up to 100 texts per request. |
| `HashEmbeddings(dim=256)` | class | Zero-dep fallback. Deterministic char-trigram hash. For tests + dev. |
| `VectorHit(id, text, score, metadata)` | dataclass | One search result. `score` is cosine similarity in `[-1, 1]`. |
| `VectorStore(embeddings)` | class | In-memory store. Methods: `add(texts, ids=None, metadata=None) -> ids`, `search(query, top_k=5, min_score=0.0) -> [VectorHit]`, `delete(ids) -> count`, `clear()`, `save(path)`, `VectorStore.load(path, embeddings=...)`, `len(store)`. numpy-accelerated when available. |
| `SemanticMemory(embeddings, recent_tail=6, top_k=4, min_score=0.15, preserve_system=True)` | class | `BaseMemory` implementation. Every message goes into a `VectorStore`; `get_messages()` returns system + retrieved-context + recent-tail. Call `set_query(text)` before each turn to focus retrieval. Access underlying store via `.store`. |
| `vector_search_tool(store, name="vector_search", default_top_k=5, max_top_k=20, description=None)` | factory | Returns a `StructuredTool` the agent can call. Args schema: `{query, top_k, min_score}`. |

`create_semantic_memory(embeddings=None, recent_tail=6, top_k=4)` — factory in `agentx_dev.Memory`. When `embeddings=None`, defaults to `HashEmbeddings(256)` so tests work with no keys.

**4. Agent-to-agent handoffs (`agentx_dev.Handoffs`)**

New module. Exports:

| Symbol | Kind | What it is |
|---|---|---|
| `HandoffRequest(target, task="", context={})` | dataclass | Sentinel returned by a handoff tool. String form: `"HANDOFF -> <target>: <task>"` — coordinator detects this in `ToolCall.result`. |
| `handoff_tool(target, description=None, tool_name=None)` | factory | Returns a `StructuredTool` named `handoff_to_<target>`. Args schema: `{task, rationale}`. |
| `HandoffCoordinator(agents: dict, entry: str, max_hops=8)` | class | Runs the routing loop. `.run(query, chat_history=None) -> HandoffResult`, `.arun(...)` (async). Chat history propagates across hops. |
| `HandoffResult(completion, hops)` | dataclass | Wraps the final `AgentCompletion` + ordered list of routing hops (`[{from, to, task, hop}, ...]`). Convenience props: `.content`, `.tool_calls`. |
| `MAX_HANDOFF_HOPS = 8` | constant | Default hop cap. |

**5. Evals harness (`agentx_dev.Evals`)**

New module. Exports:

| Symbol | Kind | What it is |
|---|---|---|
| `EvalCase(name, input, assertions=[], tags=[], metadata={})` | dataclass | One test case. |
| `EvalResult(case, completion, passed, duration_sec, ...)` | dataclass | Outcome of one case. Also tracks `input_tokens`, `output_tokens`, `cache_read_tokens`, `tool_error_count`. |
| `EvalReport(results)` | dataclass | Aggregate. Props: `passed`, `failed`, `total`, `pass_rate`, `total_duration_sec`, `total_input_tokens`, `total_output_tokens`, `total_cache_read_tokens`. Methods: `.summary() -> str`, `.to_dict()` for CI. |
| `EvalRunner(runner_factory, verbose=True)` | class | `.run(cases) -> EvalReport`. Builds a FRESH runner per case via `runner_factory` so state doesn't leak. |

Assertion helpers (all return an `(AgentCompletion) -> (passed, message)` callable):

| Helper | Signature | Checks |
|---|---|---|
| `contains(needle, case_sensitive=False)` | | Final answer contains substring. |
| `not_contains(needle, case_sensitive=False)` | | Final answer does NOT contain substring. |
| `matches_regex(pattern, flags=re.IGNORECASE)` | | Final answer matches `re.search`. |
| `called_tool(name)` | | Tool with that name was called at least once. |
| `tool_count(*, min=0, max=None)` | | Total tool calls within bounds. |
| `max_iterations(limit)` | | Steps taken `<= limit`. |
| `llm_judge(judge_model, criterion)` | | Uses another chat model to grade yes/no. |

`AssertionFn` type alias exported for typing custom assertions.

Case loaders (declarative JSON files):

| Function | What it loads |
|---|---|
| `load_case_from_dict(data: dict) -> EvalCase` | Recognizes `expected_substrings`, `forbidden_substrings`, `matches_regex`, `must_call_tools`, `max_iterations`. |
| `load_cases_from_dir(path, pattern="*.eval.json") -> list[EvalCase]` | Walks a directory. Each file is one case dict, a list of dicts, or `{"cases": [...]}`. |

CLI:

```bash
python -m agentx_dev.Evals run tests/evals/ --config agent.yaml [--pattern '*.eval.json'] [--json-out report.json]
```

Exits `0` on all-pass, `1` on any failure. Prints `EvalReport.summary()` to stdout.

**6. Package exports (`agentx_dev`)**

Everything above is re-exported at the top level. New entries in `__all__`:

```
create_semantic_memory,
Embeddings, OpenAIEmbeddings, HashEmbeddings,
VectorStore, VectorHit, SemanticMemory, vector_search_tool,
HandoffRequest, HandoffResult, HandoffCoordinator, handoff_tool,
EvalCase, EvalResult, EvalReport, EvalRunner,
contains, not_contains, matches_regex,
called_tool, tool_count, max_iterations, llm_judge,
load_case_from_dict, load_cases_from_dir,
```

**7. Runnable demo**

`examples/v3_1_features_demo.py` walks every 3.1 feature. Runs offline via
`HashEmbeddings` + mock runners for the RAG / memory / handoffs / evals
sections. Live sections (prompt caching, parallel tools) fall back to GPT
when only `OPENAI_API_KEY` is set; use Claude when `ANTHROPIC_API_KEY` is
set (prompt caching stays Anthropic-only).

---

Quick tour:

```python
from agentx_dev import (
    Claude, AgentRunner, AgentType, Permissions,
    VectorStore, HashEmbeddings, vector_search_tool,
    SemanticMemory, HandoffCoordinator, handoff_tool,
    EvalCase, EvalRunner, contains, called_tool,
)

# Prompt caching -- opt in on the model.
llm = Claude(model="claude-sonnet-4-6", enable_prompt_cache=True)

# RAG -- build a private store and hand the search tool to the agent.
store = VectorStore(embeddings=HashEmbeddings())
store.add(["Postgres uses MVCC for isolation.",
           "SQLite stores the whole DB in one file."])
runner = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[vector_search_tool(store)],
    permissions=Permissions.read_only(["./"]),
)
result = runner.invoke("How does Postgres handle concurrent writes?")

# Parallel tool calls -- native binding + multi-worker dispatch.
fast = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[weather_tool, stock_tool, news_tool],
    bind_tools_natively=True, parallel_tool_workers=4,
)

# Handoffs -- specialists that can hand back and forth.
coord = HandoffCoordinator({
    "triage": triage_runner,
    "researcher": researcher_runner,
    "writer": writer_runner,
}, entry="triage")
answer = coord.run("Write a 200-word summary of MVCC.")

# Evals -- catch regressions before shipping.
cases = [
    EvalCase("capital", "What's the capital of France?",
             assertions=[contains("Paris")]),
    EvalCase("math", "137 * 91?",
             assertions=[called_tool("calculator"), contains("12467")]),
]
report = EvalRunner(lambda: build_runner()).run(cases)
print(report.summary())
```

---

## What's new in 3.0.6 — security hardening release

All seven findings from the third-party framework audit are now fixed.
Every change is backwards-compatible: existing code keeps working; the
new defaults are strictly safer.

| Fix | What changed |
|---|---|
| **SSRF guard on `web_fetch`** | Rejects non-public destinations (loopback, RFC1918, 169.254.169.254 cloud-metadata, link-local, multicast) *and* re-validates every redirect hop. `WebTools.py` |
| **`SpawnConfig.auto_spawn_allowed_caps`** | New allow-list gates which capabilities `auto_spawn=True` will silently grant. Prompt-injected tasks can no longer talk the planner into a `["code"]` or `["delete"]` spawn. Recommended: `{"web"}`. |
| **Scrubbed subprocess env** | `run_python` / `run_shell` no longer inherit the full parent env. `print(os.environ)` inside `execute_python` cannot exfiltrate `OPENAI_API_KEY`, `AWS_*`, etc. Opt vars back in with `Permissions(subprocess_env_passthrough=[...])`; use `["*"]` for the pre-3.0.6 behaviour. |
| **HMAC-signed persistent state** | Each `run_python` build generates a per-run HMAC key, kept in parent memory. A tampered or attacker-dropped `.run_python_state.pkl` fails verification and the header starts fresh instead of unpickling attacker `__reduce__` payloads. |
| **`session_id` sanitizer** | `mint_session_dir` / `Permissions.new_session` now reject path separators, `..`, control chars, leading dots. `session_id="../../../tmp/pwn"` no longer escapes `base`. |
| **`permissions.json` mode 0o600** | Config file is now written via `os.open(..., 0o600)`, blocking multi-user hijack where another local user pre-creates the config with `execute_python=True`. |
| **ReDoS guard on `grep`** | 500-char pattern cap + 10s total-scan wall-clock budget. A catastrophic-backtracking regex from an untrusted planner can no longer freeze the agent. |
| **`invoke` / `ainvoke` accept a bare string** | `BaseChatModel.invoke("hi")` now wraps to `[{"role":"user","content":"hi"}]` — same behaviour `with_structured_output(...).invoke("hi")` has always had. Silently forwarding a string to the provider used to produce an OpenAI `Invalid type for 'messages'` 400 that read like a caller-payload problem. Lists and `{"messages": [...]}` dicts pass through unchanged. Fully additive. |
| **`AgentRunner.invoke` / `AsyncAgentRunner.ainvoke` accept a message list** | Parallel fix for the runner. Callers thinking in chat-model shapes can now pass `runner.invoke([{"role":"user","content":"prior"}, {"role":"assistant","content":"ok"}, {"role":"user","content":"NOW"}])` — the last user turn becomes the query and earlier non-system turns become the chat history (merged with any explicit `chat_history=`). Bare strings still work (classic path). `system` messages in the list are dropped because the runner assembles its own from the AgentType template + `system_addendum` + sandbox hint. `stream` / `astream` accept the same shapes. |

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
