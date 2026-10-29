# What's new

The newest release first. For what to change when you upgrade, see [Upgrading](guides/upgrading.md); every
change in detail is in the [changelog](https://github.com/shadrach098/agentx_dev/blob/main/CHANGELOG.md).

## Next release — skills

An agent can now read short **skills**: guides for a kind of work ("how to explore a project", "how to change
files safely"). The prompt carries one line per skill; the model opens the full text only when it needs it, by
calling a `use_skill` tool. The prompt stays small and is built the same way every time, so the provider's prompt
cache can reuse it. It works the same with GPT and Claude.

```python
from agentx_dev import AgentRunner, AgentType, Permissions

runner = AgentRunner(model=llm, agent=AgentType.ReAct,
                     permissions=Permissions.full_access(["./project"]),
                     skills=True)        # ~/.agentx/skills; the built-in skills are created there on first use
```

| Feature | What you get |
|---|---|
| **Built-in skills** | Six ship with agentx and are created on first use: `exploring-a-project`, `changing-files`, `running-code`, `web-research`, `delegating-work`, `auditing-a-project`. Together they cover every default tool. A skill is listed only when the agent has at least one of its tools. Your edits are never overwritten and a skill you delete stays deleted. |
| **Your own tools** | Tools no skill covers are described in one generated skill, `custom-tools`, built in memory and never written to disk. |
| **Install from a link** | In `agentx`: `/skills install https://github.com/obra/superpowers` (or a folder). The exact commit is downloaded, you are shown what was found and asked before anything is saved. `/skills` lists, `/skills remove <name>` deletes. Skills written for Claude work as they are. |
| **Use one from the prompt** | In `agentx`, type `/`: the pop-up lists the commands, then the skills (`/changing-files`, `/exploring-a-project`, ...). Pick one, add what you want (`/changing-files rename the helper to util`) and the agent reads the skill first. `/skills` lists them all. |
| **Smaller requests** | In `agentx`, old tool results are sent as a one-line stub; a file's stub names the file and says to grep it or read only the lines needed, so the agent does not read it all again. The newest results stay whole (20,000 to 40,000 characters, team helpers too), and the cut moves in steps so the start of each request stays the same and the provider's prompt cache bills it at its cheaper rate. Once a conversation is long, older skill texts become a note on how to reopen them. Big files are read in parts of 30,000 characters, command output is cut at 30,000, a skill is sent once per conversation, and summarising starts at about 15,000 tokens. In code: `trim_results=True` or a `TrimPolicy`, `Permissions.read_max_chars`. `/usage` shows what your sessions use and how much came from the cache. |
| **Skills first** | The skill list tells the model to check it before choosing its tools and to prefer a skill you installed when one fits; your own skills are listed ahead of the built-in ones. |
| **A token limit per message** | In `agentx`, a message that has used 200,000 tokens (every model, team helpers included) stops and asks before it goes on; a no wraps up, and the agent answers with what it has found. `/limit 400k` changes it, `/limit 0` turns it off, `"tokenLimit"` sets it in settings (a project may only lower it). In code: `model.on_usage = fn` is called after every reply. |
| **Web search in `agentx`** | `web_search` and `web_fetch` (readable page text, at most 30,000 characters, marked as data from the internet). The `web-research` skill is listed and the team's explorer does web research. `default` and `plan` mode ask before each lookup. |
| **No more long hangs** | A model request waits up to 5 minutes, a timeout is retried once at most, a rate limit waits as long as asked, and every retry is shown. One stuck request used to hold the spinner for about 12 minutes. |
| **MCP servers in `agentx`** | `/mcp add <name> <url>` (or a program on your computer), `/mcp login` signs in through the browser and keeps the sign-in in your system's credential store, renewed by itself. The tools reach the agent and the team helpers as `<name>__<tool>`: the ones the server marks read-only run without asking, the rest ask first (`/mcp tools`, `/mcp allow`, `/mcp ask`). A server that takes a token instead (GitHub) reads it from an environment variable. Only you can add a server. |
| **A new start screen** | `agentx` opens with a box (the mark, model, folder, tips for this folder, what's new), warnings on one line each, and the mode under the input: `⏵⏵ auto mode on (shift+tab to cycle) · model · folder`. |
| **Team jobs, faster and cheaper** | In `agentx`, explorer steps that do not depend on each other run side by side (up to 3); a step that edits or runs commands still runs alone, and every prompt still comes one at a time from the main thread. A helper that reads the same unchanged file whole a third time gets a note instead, each step may use one token limit's worth (at least 100,000) before it is told to report, and the team's report reaches the chat whole (up to 20,000 characters, was 6,000). The limit question takes `a`: go on and do not ask again in this message. |
| **Full-screen mode** | `agentx --fullscreen` (or `/fullscreen on`): the welcome box, the input and the status line stay put while only the conversation scrolls (mouse wheel, PgUp / PgDn). Questions stay above the input, and you can type while the agent works. Off by default. |
| **Looking around is one tree** | In `agentx`, skill reads, recall, reads and searches in a row are one tree (`● Skills  using-superpowers` / `├─ recall …  ⎿ Found 2 notes` / `└─ read_path …  ⎿ Read lines 1-60`), redrawn in place with a blinking yellow dot and the running step blinking white, then printed with a green dot. The agent's narration between them is not printed, and a skill's text never is. |
| **Helpers in the background** | A team's helpers work in the background: their calls are not printed. A block above the spinner, redrawn in place, shows each running step (`├ explorer · security audit  Read 14 files · Searched 6 patterns  1m 12s · 38k tokens`); a finished step leaves one line (`✓ explorer · security audit  Done (20 tool uses · 41k tokens · 1m 30s)`) and the start of its report. `/team log` lists every call. |
| **Helpers the supervisor makes** | Besides explorer, coder and runner, a team's supervisor may make a helper for a step: a name (`security-reviewer`), instructions, and tools from agentx's gated set (never delete, move or another team). It may run on any of your helper models (`security-reviewer@gpt-6-luna`). The plan shows it and its tools first; a reading one runs beside other readers. Up to 6 per job. |
| **An audit skill** | `auditing-a-project`: map the project, run its own checks, read it by area with explorers side by side, verify the findings in the code, then report Fix first / Medium / Low / Done well / Checks / Test gaps. `/auditing-a-project` starts one. |
| **Input colours** | In `agentx`, the `/name` you type is coloured (command cyan, skill magenta, unknown red) and the selected row of the `/` pop-up is highlighted. |
| **Notes and recall** | `agentx` writes a readable note per worthwhile turn to `.agentx/notes/` and indexes it (OpenAI embeddings with `OPENAI_API_KEY`, local search otherwise). The agent searches them with a `recall` tool, a fresh conversation starts with the closest ones, and `/memory [words]` shows or searches them. |
| **Safe by default** | Only you can install a skill: the agent has no tool for it and its file tools cannot write to `.agentx` or `~/.agentx`. Downloads are unpacked with path, link and size limits. A project's own skills (`.agentx/skills/`) are marked as untrusted and cannot replace yours. |
| **Agents can look at pictures** | `read_path` on a PNG, JPEG, GIF or WebP file shows the model the picture itself, on Claude and GPT, so an agent or a team helper that takes a screenshot (Playwright, say) can see how the page really looks. In `agentx`, point it at one by its location; the tool line reads `⎿ Picture, 1440x900`. Big pictures are made smaller or refused; each request carries the newest three. See [Pictures](guides/cli.md#what-it-can-do). |
| **`/voice`** | In `agentx`, type `/voice`, speak, press Enter: the words land in the input line to check before you send them. OpenAI's speech-to-text writes them out (Claude takes no audio), so it needs `OPENAI_API_KEY`; the microphone needs `pip install "agentx-dev[voice]"`. See [Voice](guides/cli.md#voice). |
| **Type while it works** | In `agentx`, what you type while the spinner runs goes to the agent at its next step (to a team job's helpers during one); it waits under the spinner as `◌ ...` and leaves one line once read: `❯ text  ✓ read by <helper>`. `/skip <step>` drops a team step not started yet. |
| **Team models shared** | With Claude and GPT models in `teamModels`, a team job gives at least one step to each provider and shares the rest by each model's note; steps ready together start in plan order; a step goes only to a helper with every tool it needs. The spinner shows minutes (`11m 23s`). |
| **Opt in** | `skills=None` (the default) changes nothing for existing code. The terminal app turns skills on. |

See the [Skills guide](guides/skills.md) and [Upgrading](guides/upgrading.md).

## 3.7 — the `agentx` terminal assistant

`pip install agentx-dev` now also installs `agentx`, a coding assistant for your
terminal, built on this framework. Run it in a project folder and ask for changes: it reads
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
