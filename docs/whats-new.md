# What's new

The newest release first. For what to change when you upgrade, see [Upgrading](guides/upgrading.md); every
change in detail is in the [changelog](https://github.com/shadrach098/agentx_dev/blob/main/CHANGELOG.md).

## 3.7 — the `agentx` terminal assistant

*(3.7.0 is not released yet.)* `pip install agentx-dev` now also installs `agentx`, a coding assistant for your
terminal, like Claude Code but built on this framework. Run it in a project folder and ask for changes: it reads
files, edits them and runs commands there, and asks you first. It works with GPT and Claude, from
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

```bash
pip install agentx-dev          # Claude also needs: pip install "agentx-dev[anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY
cd my-project
agentx                          # or: python -m agentx_dev
```

| Feature | What you get |
|---|---|
| **Approvals and modes** | Reads run; edits, commands, delete and move ask first, with a diff or the full command shown. Modes `default`, `accept-edits`, `plan` and `auto` (set on purpose only, never from Shift+Tab or a project file). `a` ("don't ask again") remembers all edits, or a plain command prefix, for the conversation. |
| **Trust a folder** | The first start in a folder asks whether you trust it. Nothing from the folder (`AGENTX.md`, its settings) is read before you say yes. `.git`, `.agentx` and shell/git config files are never written, in any mode. |
| **`AGENTX.md`** | Instructions for the model (how to run the tests, the layout, conventions), read from your home folder and from the working folder up. `/init` drafts one. |
| **Sessions** | Conversations autosave. `--continue`, `--resume [ID]` and `/resume` pick one up again. |
| **Slash commands** | `/help`, `/clear`, `/model`, `/mode`, `/settings`, `/resume`, `/sessions`, `/init`, `/team`, `/team-models`, `/exit`. Typing `/` shows the list. |
| **Team jobs** | For a big job the agent calls `start_team_job` (or you type `/team <task>`): a supervisor plans up to six steps and three helpers (explorer reads, coder edits, runner runs commands) carry them out through the same approval gate. One message may run up to 5 team jobs, so a big task continues batch after batch. A helper that runs out of its 100 steps hands on its partial work, with every file it changed, and the step after it still runs. |
| **Helper models** | `teamModels` in the settings, or `/team-models gpt-6.1-sol="hard coding" gpt-5.5`, lets the supervisor choose per step which model a helper runs on (`coder@claude-opus-5-5`); Claude and GPT can be mixed. Every model gets the same wording, so none is favoured. Each model can carry a note on what it is for; without one, the chat model writes an `auto:` note (`/team-models describe` asks again). |
| **Project memory** | Each folder keeps its own model (`/model <name> --folder`) and a work log in `.agentx/log.md`. A fresh conversation starts from the newest log entries, and a very long conversation is summarised before it overflows. |

Read the [agentx guide](guides/cli.md). Recipes: [cookbook patterns 34–37](cookbook/patterns.md), the
[FAQ](cookbook/faq.md) and [troubleshooting](cookbook/troubleshooting.md). Not in this version: token-by-token
output, a headless mode (`agentx -p`), MCP configuration, custom commands and hooks.

### Framework changes

These help anyone using the library, not only the command:

- **`Session.stream(user_input, **kw)`** is like `Session.invoke` but yields the runner's events and records the
  result on the final `completion` event.
- **Conversations continue after a final answer given as a function call** (`React_` / `respond`). Before, the
  next turn was rejected by OpenAI ("An assistant message with 'tool_calls' must be followed by tool messages").
  The history a run returns now closes that call, and an older saved history is repaired when resumed.
- **`edit_file` keeps a file's line endings.** It used to rewrite them (LF to CRLF on Windows, CRLF to LF
  elsewhere); now every byte outside the edit stays.
- **New base dependency:** `prompt_toolkit>=3.0`.

### Upgrading to 3.7

Nothing breaks. `agentx` is a new console script installed with the package; your code is not affected. See
[Upgrading](guides/upgrading.md).

## 3.6 — sub-agents

| Feature | What you get |
|---|---|
| **Sub-agents** | The planner can define a helper inline (`new_agent`: name, instructions, tools) and specialists can `delegate` a side job to a fresh agent and get a summary back. One ceiling you set bounds every helper: `SpawnConfig(capabilities={"web", "files_read"})`. [Guide](guides/sub-agents.md). |
| **Agents that keep working** | `persistence=Persistence(max_minutes=60)` on a runner or Supervisor: it reflects and changes approach when stuck, compacts long histories, waits out transient provider errors, and stops only when done or out of time or budget. Every run now reports an `outcome`. [Guide](guides/long-running-agents.md). |
| **Images, PDFs & audio** | `runner.invoke("Describe this", media=["photo.jpg"])`. One `Media` type, rendered for whichever model receives it: the same code runs on `GPT()` and `Claude()`. |
| **Models that adapt** | When a model rejects a parameter (`reasoning_effort="none"`, `max_tokens` on a reasoning model, `top_p` next to `temperature`), it adjusts, retries, and remembers the fix for that model. `adapt_params=False` opts out. |
| **More Claude settings** | `top_p`, `top_k`, `thinking` and `stop_sequences`. `temperature` is only sent when you set it. |
| **Workspace paths** | A leading `/` means the workspace root, and `run_python` starts in the workspace, so `open("notes.txt")` finds it. |
| **Dependency DAGs (3.3)** | Supervisor plans declare `depends_on`; independent steps run concurrently and each step sees only its dependencies' results. |

Upgrading: nothing breaks; two defaults changed. See [Upgrading](guides/upgrading.md).

## Earlier releases

3.5 (agents that keep working), 3.4 (media input and models that adapt), 3.3 (dependency DAGs for the
Supervisor), 3.2 (typed multi-agent pipelines), 3.1 (power features) and 3.0.6 (security hardening) are
described in the [project README](https://github.com/shadrach098/agentx_dev#readme) and, in full, in the
[changelog](https://github.com/shadrach098/agentx_dev/blob/main/CHANGELOG.md).
