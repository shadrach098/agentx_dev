# Tools

A tool is a Python function the LLM can call. This doc covers:

0. **How to actually plug tools into an agent** — auto vs manual, complete runnable code for each path.
1. The **four tool wrapper classes** (`StandardTool`, `StructuredTool`,
   `AsyncStandardTool`, `AsyncStructuredTool`) — how to author your own.
2. The **complete inventory of every built-in tool** the framework ships,
   with signatures, use cases, and when to prefer each over the others.
3. **Per-tool controls** — timeout, circuit breaker, caching, observability.
4. **Concurrent dispatch** — how multiple tool calls per turn behave.

---

## 0. How each built-in tool is registered

The framework uses **two registration paths**. Knowing which is which is
the difference between "why isn't my agent seeing the search tool?" and
a working agent.

| Tool type | Registered how | Requires |
|---|---|---|
| **DefaultTools** (file / code / shell) | **Auto** when you pass `permissions=Permissions(...)` | Set the capability flag to `True` |
| **WebTools** (`web_search_tool`, `web_fetch_tool`) | **Manual** — you import + pass in `tools=[...]` | Nothing (no API keys) |
| **RAG** (`vector_search_tool`) | **Manual** — factory bound to a `VectorStore` | You build the store |
| **Handoffs** (`handoff_tool`) | **Manual** — factory bound to a target agent name | `HandoffCoordinator` at the top |
| **Your own** (`StandardTool` / `StructuredTool`) | **Manual** — you construct + pass in `tools=[...]` | Nothing |
| **`respond`** | **Auto** by the framework when `bind_tools_natively=True` | Sets the mode |
| **`batch_concurrent`** | **Auto** by `AsyncAgentRunner` when async tools present | Register any `AsyncStandardTool` / `AsyncStructuredTool` |

The four snippets below cover every combination you'll actually need.

### 0.1 DefaultTools — auto-registered from Permissions

Pass `permissions=Permissions(...)`. The framework calls
`DefaultTools.build(permissions)` internally and inserts every tool
whose capability flag is `True`. Denied capabilities aren't refused at
call time — they don't exist as options for the LLM.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True,           # -> registers read_path
        list_directories=True,     # -> registers list_directory + find_files
        write_files=True,          # -> registers write_file
        execute_python=True,       # -> registers run_python
        # Everything else stays False -- edit_file, delete_path,
        # move_file, grep, run_shell are NOT registered.
        allowed_paths=["./workspace"],
        workspace="./workspace",
    ),
)

result = runner.invoke("Read notes.md and write a summary to summary.md.")
```

**Preset shortcuts** for the common cases:

```python
Permissions.deny_all()                        # default -- nothing registered
Permissions.read_only(allowed_paths=["./docs"])  # read + list + grep + find only
Permissions.full_access(["./workspace"])          # all 10 default tools registered
```

### 0.2 WebTools — manual, pass in the `tools=` list

Not registered by permissions. Import the factory and pass the built
tool in the runner's `tools=[...]` argument.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    web_search_tool, web_fetch_tool,
)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[
        web_search_tool(),                          # DuckDuckGo + Wikipedia fallback
        web_fetch_tool(cache_dir="./workspace"),    # optional disk cache
    ],
)
result = runner.invoke("Look up the latest Postgres release notes.")
```

No API keys needed. `web_fetch_tool` is SSRF-guarded (rejects loopback,
RFC1918, cloud-metadata endpoints). With `cache_dir=`, the full response
body lands on disk and the tool reply includes a `open(cached_path).read()`
snippet so `run_python` can process it without paying LLM tokens on the
HTML.

**Combined with DefaultTools** — pass both:

```python
runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[web_search_tool(), web_fetch_tool()],   # extra tools
    permissions=Permissions.full_access(["./workspace"]),  # DefaultTools too
)
```

The framework merges both lists into the runner's registry. Name
collisions (e.g. you named your custom tool `read_path`) raise
`TypeError` at construction so you catch it early.

### 0.3 RAG (`vector_search_tool`) — manual, bound to a store

You build a `VectorStore` first (with a `TextSplitter` for corpus
ingestion), then pass the factory-produced tool in `tools=[...]`.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    TextSplitter, VectorStore, OpenAIEmbeddings,
    vector_search_tool,
)

# 1. Build the store once (offline).
splitter = TextSplitter(chunk_size=1000, chunk_overlap=200)
docs = splitter.split_directory("./data/kb", glob="**/*.md")

store = VectorStore(embeddings=OpenAIEmbeddings())
store.add_documents(docs)
store.save("./data/kb.json")

# 2. Give the agent a search tool bound to it.
runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[
        vector_search_tool(
            store, name="kb_search", default_top_k=4,
            description="Search internal knowledge base for policy questions.",
        ),
    ],
)
```

Swap `VectorStore` for `ChromaVectorStore`, `QdrantVectorStore`, or
`PgVectorStore` at production scale — the tool factory takes any of
them (see [vector store adapters](../advanced/vector-store-adapters.md)).

### 0.4 Handoffs (`handoff_tool`) — manual, requires a coordinator

Each `handoff_tool(target)` returns a `StructuredTool` that packages
a `HandoffRequest` sentinel. `HandoffCoordinator` catches the sentinel
in one agent's tool results and re-invokes the target agent.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    handoff_tool, HandoffCoordinator,
)

triage = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[
        handoff_tool("researcher", "Use for framework docs questions."),
        handoff_tool("writer",     "Use for prose composition."),
    ],
)
researcher = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[
    handoff_tool("writer"),   # can hand off to writer too
])
writer = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

coord = HandoffCoordinator(
    agents={"triage": triage, "researcher": researcher, "writer": writer},
    entry="triage", max_hops=6,
)
result = coord.run("Write a 200-word summary of MVCC.")
```

### 0.5 Your own custom tool — manual, `StructuredTool` recipe

Anything the framework doesn't ship, you build with `StructuredTool` or
`StandardTool` and pass in `tools=[...]`. See §7 below and
[custom tools](../guides/custom-tools.md) for the full walkthrough.

```python
from pydantic import BaseModel, Field
from agentx_dev import AgentRunner, AgentType, Claude, StructuredTool

class WeatherArgs(BaseModel):
    city: str = Field(..., description="City name")

def get_weather(city: str) -> str:
    return f"22C sunny in {city}"

weather_tool = StructuredTool(
    func=get_weather, args_schema=WeatherArgs,
    name="get_weather",
    description="Get current weather for a city.",
)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[weather_tool],
)
```

### 0.6 Everything at once — the fully-loaded runner

Real production runners combine all of the above. The runner merges
`tools=[...]` (manual) with the tools built from `permissions=` (auto)
into a single registry the LLM sees:

```python
runner = AgentRunner(
    model=Claude(enable_prompt_cache=True),
    agent=AgentType.ReAct,
    tools=[
        weather_tool,                          # custom
        web_search_tool(),                     # opt-in web
        web_fetch_tool(cache_dir="./workspace"),
        vector_search_tool(kb_store),          # RAG
        handoff_tool("expert"),                # multi-agent
    ],
    permissions=Permissions.full_access(["./workspace"]),  # 10 DefaultTools
)
```

The LLM sees ~15 tools — 10 DefaultTools + 5 you passed in — and picks
between them based on each tool's `description`. Description quality
matters more than count; §6 covers what to write.

### 0.7 Rules

- **You can pass both** `tools=[...]` **and** `permissions=` at once. The
  framework merges them.
- **Name collisions raise `TypeError`** at runner construction, not at
  call time. If your custom `read_path` collides with the DefaultTools
  `read_path`, you get told immediately.
- **Denied capabilities aren't options.** The LLM literally cannot call
  a tool whose capability flag is `False` — the tool doesn't exist in
  its tool schema. This is the security model, not a policy.
- **Async tools** (`AsyncStandardTool` / `AsyncStructuredTool`) work in
  either `AgentRunner` (dispatched via the event loop) or
  `AsyncAgentRunner` (native). `AsyncAgentRunner` auto-adds
  `batch_concurrent` when it sees any async tool in the list.

---

## 1. Tool wrapper classes

Every tool the LLM can call is wrapped in one of these:

| Class | Input shape | Sync/async |
|---|---|---|
| `StandardTool` | single positional string | sync |
| `StructuredTool` | Pydantic BaseModel with typed fields | sync |
| `AsyncStandardTool` | single positional string | async |
| `AsyncStructuredTool` | Pydantic BaseModel | async |

### StandardTool — one string in

```python
from agentx_dev import StandardTool

def get_weather(city: str) -> str:
    return f"22C, sunny in {city}"

weather = StandardTool(
    func=get_weather,
    name="get_weather",
    description="Get the current weather for a city.",
)
```

The LLM sees this as accepting `{"input": "<city>"}`; the framework
unwraps single-key dicts automatically so `{"city": "Paris"}` also
works.

**Use when** — one string argument suffices (translate, sentiment,
lookup, search).

### StructuredTool — multiple typed args

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class CalculatorArgs(BaseModel):
    a: float = Field(..., description="First operand")
    b: float = Field(..., description="Second operand")
    op: str = Field(..., description="One of: add / sub / mul / div")

def calculator(a: float, b: float, op: str) -> str:
    ops = {"add": a + b, "sub": a - b, "mul": a * b, "div": a / b}
    return f"{a} {op} {b} = {ops[op]}"

calc = StructuredTool(
    func=calculator,
    args_schema=CalculatorArgs,
    name="calculator",
    description="Perform basic arithmetic (add/sub/mul/div).",
)
```

Pydantic validates the LLM's args before your function runs.

**Use when** — you need multiple parameters, typed fields (int/float/
bool/enum), or nested schemas.

### Async variants

Use for I/O-bound work (HTTP, DB, disk aio):

```python
from agentx_dev import AsyncStandardTool, AsyncStructuredTool
import aiohttp

async def http_get(url: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.text()

fetch = AsyncStandardTool(func=http_get, name="http_get",
                          description="GET a URL and return the body.")
```

Async tools work with either `AsyncAgentRunner` (native) or
`AgentRunner` (dispatched via the event loop).

**Use when** — the tool actually waits on I/O and you want other calls
to run concurrently in the same turn.

---

## 2. Complete built-in tool inventory

Ten filesystem/code tools + two web tools + three specialty tools +
one auto-added tool. Each row lists what it does, its arg schema, and
when to reach for it.

### 2.1 Filesystem / code tools (`DefaultTools`)

Permission-gated — the framework only registers a tool if its
capability flag is enabled on `Permissions`. Denied capabilities
never appear as tools, so the LLM cannot call them.

#### `read_path`

- **Capability:** `read_files`
- **Args:** `path: str, offset: int = None, limit: int = None`
- **Returns:** file contents (line-numbered when `offset`/`limit` set)
- **Use when:** the agent needs to read a source file, config, or
  markdown doc. Line-numbered slices help long files.
- **Prefer over `run_shell("cat ...")`** — safer, cross-platform,
  sandboxed.

#### `list_directory`

- **Capability:** `list_directories`
- **Args:** `path: str`
- **Returns:** one-level listing (files + subdirs)
- **Use when:** the agent needs to see what's in a directory.
- **Not recursive** — use `find_files` for globs across a tree.

#### `find_files`

- **Capability:** `list_directories`
- **Args:** `glob: str, path: str = ".", max_results: int = 200`
- **Returns:** list of matching paths
- **Use when:** the agent needs to find files by name pattern
  (`"**/*.py"`, `"src/**/test_*.py"`).
- **Auto-skips noise dirs** — `.git`, `node_modules`, `__pycache__`,
  `.venv`, `dist`, `build`.

#### `grep`

- **Capability:** `read_files`
- **Args:** `pattern: str, path: str = ".", glob: str = "**/*", regex: bool = False, case_sensitive: bool = True, max_matches: int = 100`
- **Returns:** matches formatted as `file:line: match`
- **Use when:** the agent needs content search across a codebase.
- **ReDoS guard** — 500-char pattern cap + 10-second wall-clock scan
  budget, so a catastrophic-backtracking regex can't freeze the agent.

#### `write_file`

- **Capability:** `write_files`
- **Args:** `path: str, content: str, if_exists: str = 'refuse'`
- **Returns:** success / failure message
- **`if_exists` values:**
  - `'refuse'` (default) — will not overwrite an existing file the
    agent hasn't read this session
  - `'rename'` — save under a fresh name (`report_1.md`, `report_2.md`)
  - `'overwrite'` — force overwrite
  - `'append'` — add to the end
- **Use when:** writing reports, generated code, transformed data.
- **Read-before-write safety** — the `'refuse'` default protects
  against clobbering content the agent hasn't seen.

#### `edit_file`

- **Capability:** `edit_files`
- **Args:** `path: str, find: str, replace: str`
- **Returns:** success / failure with match count
- **Behavior:** exactly one occurrence of `find` must exist, or the
  edit is refused. Automatically reads the file first if not already
  read.
- **Use when:** targeted patches in existing files (rename a var, fix
  a specific line). Cheaper token-wise than reading + rewriting the
  whole file.
- **Not for:** bulk changes across many files (use `run_python` to
  script it).

#### `delete_path`

- **Capability:** `delete_files`
- **Args:** `path: str, recursive: bool = False`
- **Returns:** success / failure
- **Use when:** cleanup after generation, removing scratch files.
- **Refuses non-empty dirs** unless `recursive=True` — safety default.

#### `move_file`

- **Capability:** `move_files`
- **Args:** `source: str, destination: str`
- **Returns:** success / failure
- **Use when:** renaming, restructuring a workspace.
- **Both endpoints must be inside the sandbox.**

#### `run_python`

- **Capability:** `execute_python`
- **Args:** `code: str`
- **Returns:** captured stdout + return value
- **Isolation:** fresh subprocess. Wall-clock timeout + output-byte cap
  applied per call.
- **Use when:** computation, aggregation, data transforms, running the
  code the agent just generated, calling libraries (numpy/pandas/…)
  that aren't tool-shaped.
- **Persistent state:** with `Permissions(python_persistent_state=True)`,
  a `state` dict persists across calls (Jupyter-like workflow inside
  subprocess isolation). See
  [Permissions](permissions.md#persistent-python-state).

#### `run_shell`

- **Capability:** `execute_shell`
- **Args:** `command: str, cwd: str = None`
- **Returns:** captured stdout + stderr + exit code
- **Isolation:** fresh subprocess (bash on Unix, git-bash / PowerShell
  on Windows). Timeout + output cap.
- **Use when:** invoking system CLIs (git, curl, ffmpeg), build tools.
- **Prefer `run_python`** for portability when possible — shell
  semantics differ across OSes.

### 2.2 Web tools (`WebTools`, opt-in)

Not registered by `DefaultTools`; import and pass them explicitly.

#### `web_search_tool()` — factory

- **Import:** `from agentx_dev import web_search_tool`
- **Returns:** `StructuredTool`
- **Args to the tool:** `query: str, num_results: int = 5`
- **Backend:** DuckDuckGo with Wikipedia fallback. **No API key.**
- **Use when:** agent needs current-web info (news, docs, "who is X",
  "what is the latest…").
- **Do not use for:** exact URL fetches (use `web_fetch_tool()`) or
  the current time / weather (those need dedicated APIs).

#### `web_fetch_tool(cache_dir=None)` — factory

- **Import:** `from agentx_dev import web_fetch_tool`
- **Returns:** `StructuredTool`
- **Args to the tool:** `url: str, max_chars: int = 50000`
- **SSRF guard** — refuses non-public destinations (loopback,
  RFC1918, 169.254.169.254 cloud-metadata, link-local, multicast).
  Re-validates every redirect hop.
- **Disk cache** — with `cache_dir=`, the FULL response lands on disk
  and the tool reply includes a `open(cached_path).read()` snippet.
  The agent can drop that into `run_python` instead of pasting the
  HTML through the LLM.
- **Use when:** you know the exact URL the agent should read.

### 2.3 Specialty tools

#### `vector_search_tool(store)` — factory *(3.1)*

- **Import:** `from agentx_dev import vector_search_tool`
- **Returns:** `StructuredTool`
- **Args to the tool:** `query: str, top_k: int = 5, min_score: float = 0.0`
- **Backends accepted:** `VectorStore` (in-memory), `ChromaVectorStore`,
  `QdrantVectorStore`, `PgVectorStore`.
- **Use when:** the agent needs to look things up in a private
  knowledge base (RAG). See
  [Vector store adapters](../advanced/vector-store-adapters.md).

#### `handoff_tool(target)` — factory *(3.1)*

- **Import:** `from agentx_dev import handoff_tool`
- **Returns:** `StructuredTool`
- **Args to the tool:** `task: str, rationale: str = ""`
- **Behavior:** returns a `HandoffRequest` sentinel that
  `HandoffCoordinator` detects to route control to another agent.
- **Use when:** you're building a multi-agent flow where specialists
  can hand off mid-run. See [Handoffs](../advanced/handoffs.md).

### 2.4 Auto-registered tools

#### `respond` — auto-added when `bind_tools_natively=True`

- **Args:** `answer: str`
- **Behavior:** signals to the runner that this is the final answer;
  the loop terminates.
- **Use when:** you set `bind_tools_natively=True` on `AgentRunner` /
  `AsyncAgentRunner`. The model calls this to end the loop.

#### `batch_concurrent` — auto-added by `AsyncAgentRunner`

- **Args:** `requests: List[Dict[str, str]]` where each dict is
  `{"name": "<tool>", "input": "<args>"}`
- **Behavior:** dispatches multiple async tools concurrently via
  `asyncio.gather`.
- **Auto-registered** only when the runner has at least one async
  tool. If you don't want it, remove it after construction from
  `runner.registry`.
- **Prefer `bind_tools_natively=True`** for modern multi-tool-per-turn
  behavior; `batch_concurrent` is the meta-tool fallback route.

---

## 3. Per-tool controls

Attach these attrs on any tool instance:

```python
calc.timeout_sec = 30                    # kill dispatch after 30s
calc.circuit_breaker = CircuitBreakerConfig(
    failure_threshold=5, recovery_timeout_sec=60,
)
```

Registry-wide defaults live on `ToolRegistry`:

```python
from agentx_dev import ToolRegistry, CircuitBreakerConfig

registry = ToolRegistry(
    tools=[calc, weather],
    default_timeout_sec=30,
    circuit_breaker_config=CircuitBreakerConfig(failure_threshold=5),
)
```

The runner accepts a registry via `runner.registry = registry` after
construction (see
[Production controls](../advanced/production-controls.md)).

---

## 4. What runners give tools automatically

Every dispatched call gets, in order:

1. **Duplicate-call guard** — warn at 3 repeats, refuse at 5. Prevents
   the model from spinning on the same call.
2. **Loop-level circuit breaker** — 3 consecutive identical
   `(action, args)` pairs force-terminate the loop with a synthesized
   answer from the last successful tool result.
3. **Circuit breaker (per-tool)** — trip after N consecutive failures;
   short-circuit subsequent calls until recovery.
4. **Timeout** — kill the dispatch (sync uses `ThreadPoolExecutor`;
   async uses `asyncio.wait_for`).
5. **Cache lookup** — return prior result if `auto_cache=True` on the
   runner (or a cache is configured explicitly).
6. **Observability events** — `TOOL_CALL_START` / `TOOL_CALL_END`
   emitted if `config.observability_enabled` is on.
7. **Args signature normalization** — the framework handles OpenAI's
   `functions.` prefix and `multi_tool_use.parallel` meta-tool
   automatically.

---

## 5. Multiple tool calls per turn

**Native binding mode (recommended, 3.1)** — set
`bind_tools_natively=True` on `AgentRunner` or `AsyncAgentRunner`.
Multiple `tool_use` blocks in one LLM response dispatch concurrently
(threads on sync, `asyncio.gather` on async). See
[Parallel tool calls](../advanced/parallel-tools.md).

**Meta-tool fallback** — when async tools are registered without
native binding, `batch_concurrent` is auto-added so the model can
still batch async calls.

---

## 6. Descriptions matter

The LLM picks tools based on the description. Be specific about:

- **What** the tool does.
- **Input shape** (types + examples).
- **Output shape** (what the model gets back).
- **When to prefer it** over another tool.

Bad: `"Weather tool."`

Good:

```
Get the current weather for a city.
Input: {city: string}. Output: "<temp>C, <conditions>" string.
Prefer over web_search for weather questions -- faster and structured.
```

## 7. Authoring your own — quick recipe

```python
from pydantic import BaseModel, Field
from agentx_dev import StructuredTool

class SlackPostArgs(BaseModel):
    channel: str = Field(..., description="Channel like '#general' or user id")
    text: str = Field(..., description="Message body, markdown ok")
    thread_ts: str = Field("", description="Reply in a thread if set")

def post_to_slack(channel: str, text: str, thread_ts: str = "") -> str:
    return f"posted to {channel}"

slack = StructuredTool(
    func=post_to_slack,
    args_schema=SlackPostArgs,
    name="slack_post",
    description=(
        "Post a message to Slack. Use for one-way notifications. "
        "For interactive replies, use slack_reply instead."
    ),
)
```

For a full walkthrough with edge cases, see
[Custom tools](../guides/custom-tools.md).

---

## 8. Cheat sheet — which tool for which job

| Job | Tool |
|---|---|
| Read a source file | `read_path` |
| Find all `test_*.py` in a repo | `find_files` |
| Search for a symbol / string in code | `grep` |
| Generate + save a report | `write_file` |
| Patch one line in an existing file | `edit_file` |
| Run some computed Python | `run_python` |
| Invoke a system CLI | `run_shell` |
| Look up on the web (no URL) | `web_search_tool()` |
| Fetch a known URL | `web_fetch_tool()` |
| Look up in a private knowledge base | `vector_search_tool(store)` |
| Send tasks between specialist agents | `handoff_tool(target)` |
| Post a Slack message / call your own API | build a `StructuredTool` |
