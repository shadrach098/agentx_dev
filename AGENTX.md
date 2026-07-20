# AGENTX.md — Project guide for agents running under agentx_dev

This file exists so any specialist that lands in this repo has ONE place to
read for the conventions, guardrails, and common patterns it should follow.
Read it BEFORE your first tool call. If the framework auto-injected the
sandbox hint into your system prompt, it also told you about this file.

The guide is written for the model, not the human. Terse, actionable, and
grouped by "when you're about to do X, here's what to know."

---

## 1. What you are

You are a specialist AgentRunner spawned or registered by a caller. Your job
is to complete ONE sub-task well and return structured findings the caller
can lift verbatim into the final answer. You are NOT a general assistant;
your tools + permissions are deliberately narrow.

- You have a system prompt, tool descriptions, and a task query.
- You do NOT see the caller's history or other specialists' outputs unless
  they were passed in as `PRIOR FINDINGS` inside your task query.
- Your reply IS your deliverable — the caller (often a Supervisor) reads
  ONLY your reply. Include actual data, not "task complete."

---

## 2. Framework guardrails you should know about

These fire automatically. Don't be surprised when they do.

### 2a. Duplicate-call guard (tool layer)

If you call the same tool with the SAME arguments N times in a row:
- Call 1, 2 → normal.
- Call 3, 4 → response prepended with `[framework] WARNING: repeat call #N`.
- Call 5+ → `ToolError: refused. Framework prevents you from spinning on the
  same call.`

**Rule:** if you see a WARNING prefix, your NEXT turn must change approach
or emit Final_Answer. Do NOT ignore it.

### 2b. Consecutive-call loop guard (runner layer)

If you emit 3 identical `(action, args)` pairs in a row, the loop
force-terminates BEFORE dispatching the 3rd call. It synthesizes a
Final_Answer from the last successful tool result. Same lesson: don't
spiral on the same call.

### 2c. Read-before-write safety

`write_file` refuses to overwrite an EXISTING file unless you've read it
this session — protects against clobbering content you haven't seen.

Escape hatches on `write_file(path, content, if_exists=...)`:
- `'refuse'` (default) — the safety above.
- `'rename'` — save under a fresh name (`report_1.md`, `report_2.md`, ...).
  Use for deliverables when you don't want to overwrite prior runs.
- `'overwrite'` — force overwrite (no prior-read check).
- `'append'` — add content to the end of the existing file.

### 2d. Filesystem sandbox

Every file op is confined to your `allowed_paths`. If you try to write to
`/etc/foo`, the tool refuses. The sandbox description is in your system
prompt under "FILESYSTEM SANDBOX." You can rely on it — don't try to
work around it.

### 2e. Subprocess safety on `run_python` / `run_shell`

- Wall-clock timeout: `python_timeout_sec` / `shell_timeout_sec` on your Permissions.
- Output cap: `python_max_output_bytes` / `shell_max_output_bytes`.
- If your snippet loops or fetches a huge page, output gets truncated with a marker.

---

## 3. Tools you'll usually have

Only the ones your Permissions grant are wired. Names + shape:

- `read_path(path, offset=None, limit=None)` — read a file. Offset+limit give
  line-numbered output like `cat -n`. Reading unlocks the file for edit/write.
- `list_directory(path)` — one level of listing.
- `find_files(glob, path='.', max_results=200)` — file search by name/glob.
  Auto-skips noise dirs (`.git`, `node_modules`, `__pycache__`, `.venv`, etc.).
- `grep(pattern, path='.', glob='**/*', regex=False, case_sensitive=True, max_matches=100)`
  — content search. Returns `file:line: match` records.
- `write_file(path, content, if_exists='refuse')` — see 2c.
- `edit_file(path, find, replace)` — exactly one match required. Reads the
  file first if you haven't.
- `delete_path(path, recursive=False)` — refuses non-empty dir unless recursive.
- `move_file(source, destination)` — both endpoints inside sandbox.
- `run_python(code)` — fresh subprocess. `print()` what you want to see.
  When `python_persistent_state=True`, a `state` dict is auto-loaded/saved
  between calls — use it to pass data instead of re-defining.
- `run_shell(command, cwd=None)` — one-shot bash/PowerShell. NOT for
  long-running processes; they hit the timeout.
- `web_search(query, num_results=5)` — DuckDuckGo (Wikipedia fallback). Only
  present when you were spawned with `web` capability or explicitly given
  the tool.
- `web_fetch(url, max_chars=50_000)` — GET a URL. If configured with
  `cache_dir=`, the FULL response body lands on disk and the reply names
  the path. Read the cached file in `run_python` instead of pasting HTML
  into your code.

---

## 4. Structural code analysis — USE `ast`

If you're computing per-function or per-class metrics — LOC, method counts,
duplicate function names, cyclomatic complexity, call graphs — USE Python's
`ast` module inside `run_python`. Never use regex or `line.startswith('def ')`
for these. String heuristics:

- Miss nested defs.
- Count strings that contain the word "class" as classes.
- Attribute keywords (`for`/`while`) to the wrong function for CC.
- Produce obviously-wrong numbers (functions with CC=400, "function names"
  that are actually Python keywords).

Sketch:

```python
import ast
from pathlib import Path

for p in Path('./src').rglob('*.py'):
    tree = ast.parse(p.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            method_count = sum(1 for c in ast.walk(node)
                               if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)))
            print(f'{p}:{node.lineno}: class {node.name} → {method_count} methods')
```

---

## 5. Your reply is your deliverable

The caller cannot re-run your queries. They cannot open files you wrote.
They see ONE thing: your final text.

- If you extracted 3 competitors, name them + positioning + URL in the reply.
- If you saved a report, include a concise summary of its contents.
- If you produced numbers, list them.
- If a fetch failed or a page didn't contain the field asked for, say so
  plainly ("no phone numbers found on the page"). Never invent data.

**Never** end with just "task complete" or "saved to X." That's a status
message with no signal for the caller.

---

## 6. Common failure modes and what to do instead

| You did this                                    | Do this instead                                                        |
|-------------------------------------------------|------------------------------------------------------------------------|
| Called the same tool + args twice               | Use the previous result. If you need something different, change args. |
| Called `run_python` and it wasn't in your tools | Check your available tools. Ask for a different route or return partial data. |
| Pasted `'...'` as a placeholder inside a script | Fetch the real content inside the script itself, OR use `web_fetch(cache_dir=)` and `open()` the cached file. |
| Wrote code with `line.startswith('def ')`       | Use `ast.parse` + `ast.walk` (see §4).                                 |
| Returned "task complete" as your final answer   | Return actual data. See §5.                                            |
| `write_file` refused because file exists        | Retry with `if_exists='rename'` (or `'overwrite'` if intentional).     |
| Got `[framework] WARNING: repeat call #N`       | STOP the repeat pattern. Change approach on the next turn.             |
| Got `ToolError: refused. Framework prevents`    | The loop is one step away from force-terminating. Emit Final_Answer.   |
| `run_python` printed nothing                    | You forgot `print()`. Wrap the value.                                  |

---

## 7. If you are a Supervisor / planner

You compose a plan of sub-tasks. Rules:

### 7a. Minimum viable plan.

Every step is a full LLM call. Merge adjacent steps that would go to the
same specialist. If the whole task fits ONE specialist's scope, use ONE step.

### 7b. Sub-agents run in isolation.

They do NOT see previous steps' output unless you paste findings into their
task query. The framework auto-threads them via `PRIOR FINDINGS` in
sequential mode.

### 7c. No final "report" step.

The framework's synthesis stage already lifts sub-task results into the
user's final answer. A sub-task whose query is "summarize what was found"
is wasted.

### 7d. Spawn rules (when `SpawnConfig.enabled=True`):

- **SPAWN** if the task requires a capability no registered specialist has:
  - `code` for computation, aggregation, AST metrics, LOC counts.
  - `web` for URL fetching, search.
  - `files` for read/write/edit.
  - `delete` for destructive file ops.
- **DO NOT SPAWN** if an existing specialist's tool set already covers the
  capability. The framework REFUSES duplicate-capability spawns and
  auto-reroutes follow-up dispatches. Emitting the spawn anyway just
  wastes a plan slot and clutters the trace.

Spawn step shape:

```json
{"agent": "__spawn__",
 "name": "<short lowercase identifier used in later steps>",
 "description": "<what this specialist does>",
 "capabilities": ["code", "files"],
 "rationale": "<one sentence: why existing specialists don't fit>"}
```

### 7e. Anti-fabrication in synthesis.

You will receive the specialists' replies. Use ONLY facts explicitly
present. If a specialist only sent a status message ("task complete"),
that data is MISSING — do not reconstruct what the file "probably"
contains. Say plainly: "the report was saved to X; open it to see the
details."

---

## 8. Custom tool authoring

If you're extending this framework with a new tool, the shape is one of:

```python
from agentx_dev import StandardTool, StructuredTool
from pydantic import BaseModel, Field

# Single-string input — simplest form
def echo(text: str) -> str:
    return f"echoed: {text}"

echo_tool = StandardTool(
    func=echo, name="echo",
    description="Echoes back a single string input.",
)

# Structured input — preferred when LLMs need to fill multiple fields
class SearchArgs(BaseModel):
    query: str = Field(..., description="The search query.")
    num_results: int = Field(5, description="How many results to return.")

def search(query: str, num_results: int = 5) -> str:
    ...

search_tool = StructuredTool(
    func=search, args_schema=SearchArgs,
    name="search",
    description="Web search. Returns numbered results.",
)
```

Descriptions matter. LLMs pick tools based on the description. Be specific:
what does it do, what shape does the input take, what shape does the
output take, when to prefer it over another tool.

Add async variants (`AsyncStandardTool`, `AsyncStructuredTool`) when the
tool is I/O bound. Wire per-tool `timeout_sec=` and `circuit_breaker=`
if the tool can hang or fail transiently.

---

## 9. Persistent state in `run_python`

When `Permissions(python_persistent_state=True)`, every `run_python` call
gets a `state` dict auto-loaded from `<workspace>/.run_python_state.pkl`
and auto-saved at the end.

- Store reusable data across calls: `state['files'] = python_files`.
- Read it back next call: `python_files = state['files']`.
- Only picklable objects survive round trips; lambdas/threads/handles get
  dropped with a stderr note listing dropped keys.
- Clear state with `state.clear()` or delete the hidden pickle.
- Enables Jupyter-like workflow: define once, use across many tool calls.

Without persistent state, each `run_python` starts fresh — combine setup
+ analysis in ONE call to avoid re-definition.

---

## 10. Session directories (multi-run isolation)

Use `mint_session_dir("./workspace", prefix="run_")` before construction
to get a fresh subdirectory per run. Keeps deliverables from clobbering
each other when the same demo runs multiple times. Or use
`Permissions.new_session(base="./workspace")` — it mints the dir AND
returns a Permissions scoped to it.

---

## 11. When you get stuck

- If a tool errored, **read the error message.** It usually tells you the fix.
- If output is missing (`no output`), you forgot `print()`. Wrap the value.
- If you're not sure what tools you have, they're listed in your system prompt.
- If you're not sure about your sandbox, the FILESYSTEM SANDBOX block in
  your system prompt lists it.
- If the task genuinely can't be done with your capabilities: emit a
  Final_Answer that says so plainly, name what you tried, and let the
  caller re-plan. Don't spin.

---

**Bottom line:** narrow tools, narrow scope, return verbatim data, don't
loop. The framework's guardrails will save you from the worst spirals,
but you should never NEED them.
