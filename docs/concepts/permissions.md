# Permissions & sandbox

The framework's file/code tools (`read_path`, `write_file`, `run_python`,
etc.) are gated by a **capability model**. You describe what the agent
is allowed to do; the framework registers only the matching tools. A
denied capability isn't just refused at call time — the tool doesn't
appear as an option to the model.

## Permissions

```python
from agentx_dev import Permissions

perms = Permissions(
    read_files=True,
    list_directories=True,
    write_files=False,        # writes denied
    edit_files=False,
    delete_files=False,
    move_files=False,
    execute_python=True,      # can run code
    execute_shell=False,      # can't run shell
    allowed_paths=["./workspace", "./data"],
    python_timeout_sec=10.0,
    python_max_output_bytes=100_000,
    max_file_size_bytes=10 * 1024 * 1024,   # 10 MB
    workspace="./workspace",                # unqualified paths resolve here
)
```

## Presets

Common configurations:

```python
Permissions.deny_all()                         # default; nothing allowed
Permissions.read_only(allowed_paths=["./docs"])
Permissions.full_access(["./project"])         # read/write/exec inside sandbox
```

## Wire it into a runner

Pass `permissions=` and `DefaultTools` get auto-built:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
)
```

The framework:

1. Registers only the tools whose capability is enabled.
2. Injects a "FILESYSTEM SANDBOX" block into the system prompt telling
   the model exactly where it can operate. It also auto-discovers
   `AGENTX.md` at the workspace root and points the model at it.
3. Enforces `allowed_paths` inside every dispatch — `../../etc/passwd`
   gets blocked because path resolution flattens `../` before the
   subtree check.

## The 10 default tools

| Tool | Capability | Purpose |
|---|---|---|
| `read_path` | `read_files` | Read a file. Supports offset/limit for line-numbered slices. |
| `list_directory` | `list_directories` | One-level listing. |
| `find_files` | `list_directories` | Glob search; auto-skips `.git`, `node_modules`, `.venv`, `__pycache__`, etc. |
| `grep` | `read_files` | Content search (cross-platform, regex or literal). ReDoS guard: 500-char pattern cap, 10s scan budget. |
| `write_file` | `write_files` | Create / update. `if_exists=` controls collision behavior (`refuse` / `rename` / `overwrite` / `append`). |
| `edit_file` | `edit_files` | Replace a substring. Exactly one match required. Reads the file first if not already read. |
| `delete_path` | `delete_files` | Delete file or dir (`recursive=True` for non-empty dirs). |
| `move_file` | `move_files` | Rename / move within sandbox. |
| `run_python` | `execute_python` | Execute a Python snippet in a fresh subprocess. |
| `run_shell` | `execute_shell` | One-shot shell command (bash on Unix, git-bash / PowerShell on Windows). |

## Read-before-write safety

`write_file` refuses to overwrite an EXISTING file unless you've read
it this session. Prevents clobbering content the agent hasn't seen.

Escape hatches:

```python
write_file(path, content, if_exists='refuse')     # default
write_file(path, content, if_exists='rename')     # save as report_1.md, ...
write_file(path, content, if_exists='overwrite')  # force
write_file(path, content, if_exists='append')     # add to the end
```

## Subprocess isolation (`run_python`, `run_shell`)

- **Fresh subprocess** — the parent process isn't touched. A crashed
  snippet doesn't crash the agent.
- **Wall-clock timeout** — `python_timeout_sec` / `shell_timeout_sec`.
  A runaway loop gets killed.
- **Output cap** — `python_max_output_bytes` / `shell_max_output_bytes`.
  Truncated with a marker if exceeded.
- **Scrubbed env** (3.0.6) — the subprocess does NOT inherit the full
  parent env. `print(os.environ)` cannot exfiltrate `OPENAI_API_KEY`,
  `AWS_*`, etc. Opt vars back in with
  `Permissions(subprocess_env_passthrough=["FOO", "BAR"])` or `["*"]`
  for the pre-3.0.6 behavior.

## Persistent Python state

Turn on `python_persistent_state=True` and every `run_python` call gets
a `state` dict auto-loaded and saved between calls:

```python
perms = Permissions(
    read_files=True, write_files=True, execute_python=True,
    allowed_paths=["./workspace"], workspace="./workspace",
    python_persistent_state=True,
)

# Turn 1 -- setup:
# state['files'] = [f for f in Path('./src').rglob('*.py')]

# Turn 2 -- analysis, no redefinition needed:
# print(sum(len(open(f).read().splitlines()) for f in state['files']))
```

Backed by `.run_python_state.pkl` in the workspace. **HMAC-signed**
(3.0.6) — a tampered or attacker-dropped pickle fails verification and
starts fresh instead of unpickling a malicious `__reduce__` payload.

Non-picklable values (lambdas, threads, live handles) are silently
dropped with a stderr note listing the dropped keys.

## Session directories

Batch jobs or parallel evals need isolation between runs. Mint a fresh
subdirectory per run:

```python
from agentx_dev import mint_session_dir, Permissions

# Creates ./workspace/run_<UTC timestamp>_<8-char id>/
session_dir = mint_session_dir("./workspace", prefix="run_")

perms = Permissions(
    write_files=True, execute_python=True,
    allowed_paths=[session_dir],
    workspace=session_dir,
)
```

Or the shorthand:

```python
perms = Permissions.new_session(base="./workspace")
```

25 runs → 25 distinct output folders. Sortable by timestamp, uniquely
suffixed.

**Sanitizer (3.0.6):** `session_id` values are rejected if they contain
path separators, `..`, control chars, or a leading dot.
`session_id="../../../tmp/pwn"` cannot escape `base`.

## Config file auto-generation

```python
from agentx_dev import Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.from_file(),
)
```

First run creates `.agentx/permissions.json` with deny-all defaults.
Edit it to grant capabilities. **File mode 0o600** (3.0.6): only the
current user can read/write it.

```json
{
  "read_files": true,
  "list_directories": true,
  "write_files": true,
  "execute_python": true,
  "allowed_paths": ["./workspace"]
}
```

## AGENTX.md project guide

Drop an `AGENTX.md` file at your repo root or workspace and the runner
auto-injects a pointer into the system prompt:

> PROJECT GUIDE: an AGENTX.md file exists at &lt;path&gt;. READ IT FIRST
> via read_path — it documents the conventions this codebase expects.

The framework never inlines the file's contents (that would burn tokens
on every call). The model reads on demand.

Think of it as CLAUDE.md-style project instructions written for the
model, not for humans. See the `AGENTX.md` at the repo root for a
template.
