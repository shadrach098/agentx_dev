# What's new

The newest release first. For what to change when you upgrade, see [Upgrading](guides/upgrading.md); every
change in detail is in the [changelog](https://github.com/shadrach098/agentx_dev/blob/main/CHANGELOG.md).

## 3.7 — the `agentx` terminal assistant

`pip install agentx-dev` now also installs `agentx`, a coding assistant for your
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
| **Slash commands** | Eleven commands for the session itself (switch the model, change the mode, resume a conversation, start a team job). Typing `/` shows the list; each is explained [below](#slash-commands). |
| **Team jobs** | For a big job the agent calls `start_team_job` (or you type `/team <task>`): a supervisor plans up to six steps and three helpers (explorer reads, coder edits, runner runs commands) carry them out through the same approval gate. One message may run up to 5 team jobs, so a big task continues batch after batch. A helper that runs out of its 100 steps hands on its partial work, with every file it changed, and the step after it still runs. |
| **Helper models** | `teamModels` in the settings, or `/team-models gpt-6.1-sol="hard coding" gpt-5.5`, lets the supervisor choose per step which model a helper runs on (`coder@claude-opus-5-5`); Claude and GPT can be mixed. Every model gets the same wording, so none is favoured. Each model can carry a note on what it is for; without one, the chat model writes an `auto:` note (`/team-models describe` asks again). |
| **Project memory** | Each folder keeps its own model (`/model <name> --folder`) and a work log in `.agentx/log.md`. A fresh conversation starts from the newest log entries, and a very long conversation is summarised before it overflows. |

### Slash commands

A line that starts with `/` and a command name runs the command; it is never sent to the model. Typing `/` shows
the list with a one-line summary of each. A path such as `/README.md` or `/tmp/build.log fails` is not a command, it
goes to the model as a normal message. An unknown command prints the list.

| Command | What it does | Example |
|---|---|---|
| `/help` | Lists every command with a one-line summary. | `/help` |
| `/clear` | Saves the current conversation and starts a new one, which begins from the newest entries of the folder's work log. "Don't ask again" grants are cleared; your mode and your settings rules stay. Earlier conversations stay on disk for `/resume`. | `/clear` |
| `/model [name] [--folder]` | With no name, shows the model in use. With a name, switches to it and keeps the conversation. Without `--folder` the switch lasts for this session; with `--folder` it is also saved in `.agentx/settings.json`, so this folder starts on that model next time. A model that cannot be built (no API key, unknown name) leaves the old one in place. | `/model claude-opus-5-5 --folder` |
| `/mode [name]` | With no name, shows the mode. With a name, sets it: `default` (asks before edits and commands), `accept-edits` (edits run without asking, commands still ask), `plan` (reads only, and answers with a plan instead of changing anything) or `auto` (edits, commands and team jobs run without asking; delete and move still ask, and the deny rules and protected files still apply). `auto` is only set on purpose, never by Shift+Tab. | `/mode accept-edits` |
| `/settings` | Shows what is in force: the model, the mode, the allow and deny rules, the helper models with their notes, and the paths of your settings file and the folder's. | `/settings` |
| `/resume [id]` | Loads a saved conversation from this folder. With no id it lists them (newest first) to pick from; with an id, or its first few characters, it loads that one. | `/resume 3f9a2c1b` |
| `/sessions` | Lists this folder's saved conversations: date, the first message as a title, and the id to give `/resume`. | `/sessions` |
| `/init` | Asks the model to look at the project and draft an `AGENTX.md` (how to run the tests, the layout, conventions). The write goes through the approval prompt like any other edit. | `/init` |
| `/team <task>` | Hands a big job to a team: a supervisor plans the steps, then explorer, coder and runner helpers carry them out. You are asked once before it starts (in `auto` mode it starts straight away). With no task it prints the usage. | `/team add type hints to every module and run the tests` |
| `/team-models ...` | Shows or sets the extra models team helpers may run on. `/team-models` shows the list; `/team-models a b` sets it (up to 4 extra models besides the chat model), with an optional note on what each is for (`name="hard coding"`); `describe` asks the chat model to write the missing notes (marked `auto:`); `clear` empties the list. Add `--folder` to save it for this folder. | `/team-models gpt-6.1-sol="hard coding" claude-sonnet-5-5 --folder` |
| `/exit` | Saves the conversation and leaves. Ctrl+D does the same. | `/exit` |

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
