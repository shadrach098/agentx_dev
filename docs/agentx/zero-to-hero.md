# agentx: zero to hero

`agentx` is a coding assistant that runs in your terminal. You start it in a project folder and ask in plain words:
"why does the login page crash?", "add a dark mode toggle", "run the tests and fix what fails". It reads your files,
edits them and runs commands in that folder, and it asks you before it changes anything. It works with Claude and
GPT models, and for big jobs it can split the work across a team of helpers that run in parallel.

This page takes you from nothing installed to using every part of it, one step at a time. Each step builds on the
one before. Every detail is in the [full reference](../guides/cli.md); problems are in
[troubleshooting](../cookbook/troubleshooting.md).

---

## Part 1: zero

### 1. Install it

You need Python 3.10 or newer. Open a terminal and run:

```bash
pip install "agentx-dev[anthropic]"     # for Claude models
pip install agentx-dev                  # enough for GPT models only
```

Optional extras, any time later:

```bash
pip install "agentx-dev[voice]"         # /voice: speak instead of typing
pip install "agentx-dev[mcp]"           # MCP servers: more tools for the agent
pip install pillow                      # shrinks screenshots that are too big to send
```

If `pip` and `python` point at different installs on your machine, use `python -m pip install ...` instead.

### 2. Give it a key

`agentx` reads your API key from an environment variable: `ANTHROPIC_API_KEY` for Claude, `OPENAI_API_KEY` for
GPT. Set one (or both).

```bash
# macOS / Linux (add the line to ~/.bashrc or ~/.zshrc to keep it)
export ANTHROPIC_API_KEY=sk-ant-...

# Windows, saved for good (then open a NEW terminal)
setx ANTHROPIC_API_KEY "sk-ant-..."

# Windows PowerShell, this window only
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

With both keys set you can mix Claude and GPT models in one team job (step 16). `/voice` always needs
`OPENAI_API_KEY`, because Claude models take no audio.

### 3. Start it

```bash
cd my-project
agentx
```

The first time in a folder it asks whether you trust it:

```text
Accessing workspace:

 C:/Users/you/projects/site

Quick safety check: is this a project you created or one you trust? ...
> 1. Yes, I trust this folder
  2. No, exit
```

Nothing in the folder is read until you say yes, and it does not ask again for that folder. Then you see the start
screen (the model, the folder, tips) and the input line:

```text
────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────
 default mode: edits and commands ask first (shift+tab to cycle) · claude-opus-5-5 · site
```

Pick the model with `--model` (`agentx --model claude-opus-5-5`, `agentx --model gpt-6.1-sol`), or switch later with
`/model`. To leave, type `/exit` or press Ctrl+D; the conversation is saved.

### 4. Run `/init`: tell it about your project

Do this once per project, before anything else:

```text
❯ /init
```

`/init` asks the agent to study the project and write an **`AGENTX.md`** file in the folder: a short page of notes
(under 60 lines) that the agent reads at the start of every conversation, so it does not have to rediscover your
project each time. What happens:

1. It looks around: the top-level files, the README and the main config files (`package.json`, `pyproject.toml`,
   ...). Reading never asks.
2. It writes `AGENTX.md` with `write_file`, so you see the whole file as a diff and approve it like any edit.
3. If the folder already has an `AGENTX.md`, it tells you instead of overwriting it.

A typical result:

```markdown
# AJ Style Gallery
Online shop: React + Vite front end (src/), Hono API (server/), Supabase for login and data.

## Run it
- Website: npm run dev -- --port 5188
- API: npm --prefix server run dev
- Tests: npm test (website), npm --prefix server test (API)

## Conventions
- TypeScript strict; no new dependencies without asking.
- Never commit .env files; secrets live on the server only.
```

Edit it by hand any time: add the rules you keep repeating ("always run the tests", "we use pnpm", "do not touch
the payments code"). It is plain markdown, and short and true beats long. It is used from the next conversation
(`/clear`) and every start after that. It can be committed, so your whole team shares it. Your own
`~/.agentx/AGENTX.md` applies to every project (write your personal habits there).

---

## Part 2: everyday use

### 5. Ask a question

Start with something that changes nothing:

```text
❯ what does this project do, and how do I run it?
```

The agent looks around and you see what it did, as a small tree:

```text
● read_path  README.md
  ⎿ Read 120 lines
● grep  "def main"
  ⎿ 3 matches in 2 files searched
```

Reading, listing and searching never ask: they cannot change anything. Then the answer appears.

### 6. Make your first change

```text
❯ add a --verbose flag to the CLI
```

Before anything is written, you see exactly what will change and are asked:

```text
+- edit_file -------------------------------------------+
| Action: Edit src/cli.py                                |
| --- a/src/cli.py                                       |
| +++ b/src/cli.py                                       |
| +    parser.add_argument("--verbose", ...)             |
+--------------------------------------------------------+
Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again: all edits this session
  3. No, and tell the model what to do instead
```

- **Yes** (Enter, `1` or `y`): this one change.
- **Yes, and don't ask again** (`a`): every edit for the rest of this conversation. For a command, `a` remembers its
  first word (`pytest`, or two words such as `git status`).
- **No** (`n`): nothing happens, and you can type what it should do instead; the agent is told and adjusts.

Commands work the same way: `run_shell` shows the exact command, `run_python` shows the code. Delete and move
always ask, whatever you answered before.

### 7. Choose how much it asks: modes

Press **Shift+Tab** to cycle the mode; the line under the input shows the current one.

| Mode | What asks first |
|---|---|
| `default` | Edits, commands, web lookups, delete and move. |
| `accept-edits` | Commands, delete and move. File edits just happen. |
| `plan` | Nothing changes at all: it reads and proposes a plan. Good for "how would you do X?". |
| `auto` | Only delete, move and tools it does not know (such as MCP tools). Edits and commands just run. |

`auto` is never reached with Shift+Tab: choose it on purpose with `/mode auto` or `agentx --mode auto`, in folders you
trust. Some files are protected in every mode: `.git`, `.agentx` and your shell and git config are never edited.

### 8. Talk to it while it works

You do not have to wait. While the spinner runs (`type to add to the task`), type and press Enter:

```text
⠸ Running run_shell... 41s  · type to add to the task
◌ also check the footer  · waiting for the agent's next step
```

Your message reaches the agent at its next step; then one line is left: `❯ also check the footer  ✓ read by the agent`.

- **Changed your mind?** `/unsend` takes back the newest waiting message before the agent reads it (`/unsend 2` the
  second one listed, `/unsend all` all of them).
- **Commands that only show something run at once** while it works: `/usage`, `/settings`, `/help`, `/team log`,
  `/skills`, `/mcp` ... and `/mode plan` changes the mode at once. Commands that change things (`/model <name>`,
  `/clear`, `/init`) wait until it is done.
- **`/voice` works here too**: it starts recording at once; press Enter to stop, and the words appear in the line
  you are typing, to check and send.

- **Ctrl+C** stops the turn. Changes already made stay on disk; the stopped turn is not remembered.
- At an approval prompt, Ctrl+C just says no to that one call.
- A command that runs too long is stopped by itself: shell commands after 5 minutes, Python after 2.

### 9. The commands you will use most

Type `/` to see them all in a pop-up.

| Command | Does |
|---|---|
| `/help` | Lists the commands. |
| `/model [name]` | Shows or switches the model. `--folder` keeps it for this folder. |
| `/mode [name]` | Shows or sets the mode. |
| `/clear` | Starts a fresh conversation (the old one is saved). |
| `/resume`, `/sessions` | Pick up an earlier conversation. `agentx --continue` resumes the latest one at start. |
| `/usage` | Tokens used this session and by the last message. |
| `/limit [tokens]` | How many tokens one message may use before it asks (200,000 by default). |
| `/settings` | What is in force, and where the settings files are. |
| `/init` | Writes an `AGENTX.md` with notes about the project (step 4). |
| `/unsend` | Take back a message you typed while it works, before it is read. |
| `/exit` | Save and leave. |

Keys: **Alt+Enter** for a new line, **Up** for earlier messages, **Tab** to complete a command or a file path.

---

## Part 3: make it yours

### 10. Show it pictures

The agent can look at pictures: PNG, JPEG, GIF and WebP. Give it the file's location:

```text
❯ look at C:\Users\you\Pictures\Screenshots\login-bug.png and fix what is wrong with the layout
```

```text
● read_path  C:\Users\you\Pictures\Screenshots\login-bug.png
  ⎿ Picture, 1440x900
```

A picture inside the project opens without asking; one outside it (your Screenshots folder) asks first. The agent
can also take its own screenshots, with Playwright for example, and look at them, so "check that the page looks
right" means it really looks. A picture may be at most 5 MB; each request carries the newest three pictures.

### 11. Speak instead of typing

```text
❯ /voice
● Recording… press Enter to stop (Ctrl+C cancels)
```

Speak, press Enter, and the words appear in the input line. Check or fix them, then press Enter to send. Nothing
is sent by itself. It needs `pip install "agentx-dev[voice]"` and `OPENAI_API_KEY` (about $0.006 a minute).

### 12. Settings and rules

Two JSON files, both optional: `~/.agentx/settings.json` (yours) and `.agentx/settings.json` in the project. A
useful one:

```json
{
  "model": "claude-opus-5-5",
  "permissions": {
    "allow": ["run_shell:npm test*", "run_shell:git status*"],
    "deny":  ["run_shell:*rm -rf*", "read_path:.env"]
  }
}
```

- **allow**: these run without asking (`npm test`, `git status`).
- **deny**: these are refused in every mode, `auto` included.
- A project's file cannot switch on `auto` or `accept-edits`, and its allow rules are asked about at start, because a
  repository you cloned may contain one.

`--add-dir ../shared` (or `"addDirs"`) lets the tools work in another folder too.

---

## Part 4: power features

### 13. Skills

A skill is a short guide the agent opens when a task needs it. Six come built in (`exploring-a-project`,
`changing-files`, `running-code`, `web-research`, `delegating-work`, `auditing-a-project`). Use one yourself by name:

```text
❯ /auditing-a-project
❯ /changing-files rename the helper to util everywhere
```

Install more from GitHub; you see what is in it and are asked before anything is saved:

```text
❯ /skills install https://github.com/obra/superpowers
❯ /skills
```

More in [Skills](../guides/skills.md).

### 14. MCP servers: more tools

An MCP server gives the agent new tools: a database, a search, your company's wiki.

```text
❯ /mcp add flightclaw https://mcp.flightclaw.com/mcp
❯ /mcp login flightclaw          # sign in in the browser, if it asks
❯ /mcp tools flightclaw          # what it can do, and which tools ask first
```

Tools the server marks read-only run without asking; the rest ask, in every mode. Only you can add a server: the
agent has no way to. More in [MCP servers](../guides/mcp.md) and the [reference](../guides/cli.md#mcp-servers).

### 15. It remembers your project

In a project folder `agentx` keeps, without any setup:

- **a work log** (`.agentx/log.md`): one entry per finished message, so after a restart "what did we do here?" works;
- **notes** (`.agentx/notes/`): searchable by meaning, with the `recall` tool and `/memory <words>`;
- **a summary** of a very long conversation, so it never overflows the model.

Add `.agentx/log.md` to `.gitignore` if it should stay private.

### 16. Team jobs: helpers in parallel

For a big job with several parts (research, then changes, then tests), the agent can hand it to a team:

```text
❯ /team audit the payment code, fix what you find and run the tests
```

It asks you first (a team makes many model calls). Then a supervisor plans up to 6 steps and gives each to a helper:
**explorer** (reads and searches, also the web), **coder** (edits files) or **runner** (runs commands), or a helper
it makes for the step (`security-reviewer`). Reading steps run side by side, up to three at once:

```text
● Team · 3 running · 1 of 5 done
  ├ explorer · payments      Read 9 files · Searched 4 patterns   58s · 150k tokens
  ├ explorer · auth routes   Read 14 files                         1m 12s · 180k tokens
  └ explorer · frontend      Fetched 1 page · Read 6 files         40s · 90k tokens
```

- Type while it runs: your note goes to the helpers. `/skip 3` drops step 3 if it has not started.
- Every helper uses the same approvals as you do.
- `/team log` lists every call the last job made; each helper's full report is saved in `.agentx/team/`.

**Helpers on other models.** Let helpers run on more models, with a note on what each is for:

```text
❯ /team-models claude-sonnet-5-5="fast reading" gpt-6.1-sol="hard coding" --folder
```

The supervisor then shares the steps among them (at least one step each for Claude and GPT when you list both).
Each step costs what its model costs. More in [Team jobs](../guides/cli.md#team-jobs).

### 17. Full-screen mode

```bash
agentx --fullscreen          # or /fullscreen on, from the next start
```

The header and the input stay put while only the conversation scrolls (mouse wheel, PgUp / PgDn). Hold Shift to
select text with the mouse.

---

## Part 5: hero habits

- **Start in `plan` mode for anything big**: read its plan, then switch to `default` and say "do it".
- **Read what you approve.** The prompt shows the whole diff or command; it is your protection.
- **Keep secrets out of the project** and out of the chat. Conversations are saved as plain JSON in `~/.agentx`.
- **Watch the cost**: `/usage` after a big message, `/limit` to set when it asks.
- **Give it a way to check its work**: tests, a build, a screenshot. "Fix it and run the tests" beats "fix it".
- **Point at pictures** of what is wrong instead of describing them.
- **Use `/clear`** when you change topic: a fresh conversation is cheaper and sharper, and the work log carries over.

## Cheat sheet

```text
agentx                         start          agentx --continue        resume the latest
agentx --model NAME            pick a model   agentx --mode plan       start in plan mode
Shift+Tab   cycle modes        Alt+Enter  new line        Ctrl+C  stop the turn
/help  /model  /mode  /clear  /resume  /usage  /limit  /settings  /exit
/init  /voice  /unsend  /team <task>  /team-models  /skip <step>  /skills  /mcp  /memory  /fullscreen
```

Next: the [full reference](../guides/cli.md) has every rule and limit, and
[troubleshooting](../cookbook/troubleshooting.md#the-agentx-command-3-7) covers the common problems.
