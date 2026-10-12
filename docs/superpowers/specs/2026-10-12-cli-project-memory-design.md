# Project memory for the `agentx` command (per-folder settings, work log, compaction)

Status: design, approved section by section on 2026-10-12. Target release: 3.7.0 (unreleased, additive; no new
dependency).
Builds on: `2026-10-10-cli-coding-assistant-design.md` (the `agentx` command, settings, sessions, `AGENTX.md`
context, the gate) and `2026-10-11-cli-teams-mode-design.md`.

## 1. Problem

`agentx` keeps one global settings file and one conversation file per folder, stored under `~/.agentx/`. Four
things follow from that:

1. A project cannot have its own model. A `.agentx/settings.json` inside a folder already overrides the global
   `model`, but nothing creates it, so nobody sees it exists.
2. Nothing records what was done in a folder. `AGENTX.md` is written by hand (or drafted once by `/init`).
3. Every resumed conversation replays every old message on every turn. That costs more with each turn and
   eventually overflows the model's limit (a limit the guide lists today).
4. A fresh conversation in a folder starts knowing nothing about earlier work there, except `AGENTX.md`.

## 2. Goals and non-goals

Goals:

1. Each folder can have its own model, in a file inside the folder, created for the user.
2. The app keeps a readable work log in the folder: what was asked, which files changed, which commands ran, the
   answer. No extra model call.
3. A fresh conversation in the folder starts from the log instead of replaying old messages.
4. A long conversation is summarised before it overflows, without ever dropping messages that have no summary.
5. Nothing here widens what the model may do: the model cannot write the log or the folder settings (the
   existing protection of `.agentx/`), and everything loaded from the folder is treated as project-supplied.

Non-goals (this version): searching the log by meaning (embeddings), memory shared between folders, a manual
`/compact` command, the model editing the log, per-folder mode or permission rules (a project file cannot loosen
those anyway), changing how `--continue` and `/resume` load a saved conversation.

## 3. Approaches considered

- **A (chosen): the app writes a plain-markdown log and summarises on a size threshold.** Deterministic, cheap,
  visible, editable, and safe from the model: the log is written by Python from the turn's events, not by a tool.
- **B: let the model keep its own notes with `write_file`.** Costs tokens every turn and does not fit the rule
  that `.agentx/` is off-limits to the model.
- **C: an embedding store of past turns.** Powerful, but a new dependency and far more machinery than a folder
  of notes needs.

## 4. Structure

| Piece | Change |
|---|---|
| `agentx_dev/cli/memory.py` (new) | `TurnRecord`, `entry_text`, `append_entry`, `rotate`, `read_recent`, `log_path`. Pure functions plus file I/O; no terminal output; redaction is passed in. |
| `agentx_dev/cli/compact.py` (new) | `history_size`, `needs_compaction`, `split_point`, `transcript`, `summarise`, `apply_summary`. The model call is passed in. |
| `agentx_dev/cli/settings.py` | `project_settings_path` already exists; add `write_project_model(cwd, model)` and `ensure_project_settings(cwd, model)`. |
| `agentx_dev/cli/context.py` | `build_addendum(cwd, home, system=None, log_text="")` gains an optional `<project-log>` block. |
| `agentx_dev/cli/app.py` | Collects each turn's facts, writes the log after the turn, loads the log on a fresh conversation, checks compaction before a turn, creates the folder settings after a new trust. |
| `agentx_dev/cli/commands.py` | `/model <name> --folder`. |
| Docs | Guide section "Project memory", CHANGELOG 3.7.0, README line, checklist items, and a correction of the guide's limits list. |

None of this applies when the working folder is the user's home folder or a filesystem root (neither is a
project): no folder settings file and no log are created there.

## 5. The folder settings file

- **Created** the first time the user answers yes to the trust question for a folder (so never for a folder
  the user has not trusted, and never before anything from it is read). Contents: `{"model": "<the model in
  use>"}` and nothing else. An existing file is never touched.
- **Saved to** with `/model <name> --folder`: the model is switched for the session and written to the folder
  file, creating it if needed. Other keys in an existing file are kept. A file that is not valid JSON is not
  overwritten: the command says so and does nothing to the file. Plain `/model <name>` stays session-only, and
  its notice mentions `--folder`.
- **Precedence** is unchanged: `--model` beats the folder file, which beats the user's file, which beats the
  default chosen from the API key.
- A folder file can only set what a project file may already set (`model`, `mode`, `addDirs`, `permissions`);
  the existing rules for untrusted project settings are unchanged.
- Writes are atomic (temporary file, then replace). Failures (read-only folder) give a one-line notice and no
  traceback.

## 6. The work log (`<folder>/.agentx/log.md`)

Written by the app after each finished turn, including a turn that ran a team job. An interrupted turn writes
nothing (its history is not kept either).

An entry:

```
## 2026-10-12 14:03 - Add a docstring to each module
Changed: src/a.py, src/b.py
Ran: pytest -q
Answer: I added docstrings to 6 modules and the tests pass.
```

- **Facts come from the turn's events**, not from the model: the user's message, each `tool_call` paired with
  its `tool_result`, and the `final` text.
- **Changed** lists the paths of `write_file`, `edit_file`, `delete_path` and `move_file` calls whose result did
  not start with a refusal or an error text (`Refused`, `The user declined`, `ERROR`, `Error`); a move lists
  `source -> destination`. More than 10 paths: the first 10 and "and N more". This is a text check, so a rare
  unusual result could be misjudged; the guide says so.
- **Ran** lists `run_shell` commands (first 100 characters) and `run_python` calls (`python: <first line>`) that
  were not refused. A team job adds `Team job: <task, first 100 characters>` and its outcome word.
- A line is omitted when empty. The title is the user's message cut at 80 characters. The answer is cut at 300.
  A whole entry is capped at 1,200 characters.
- **Secrets:** requests and commands go through the app's key-hiding (the same patterns and environment key
  values as error messages) before being written. The file is made readable by the user only where the platform
  allows. It lives in the folder, so a committed folder commits it; the guide says to add it to `.gitignore` if
  it should stay private. The app never edits `.gitignore`.
- **Size:** above 200 KB, the oldest half of the entries is moved to `log-older.md` (appended) and the log is
  rewritten atomically.
- **Failures** (unwritable folder, a full disk) give one notice per session and never stop the chat.
- The model cannot write here: `.agentx/*` is on the always-on deny list, and the app writes with Python, not
  through a tool.

## 7. Starting from the log

- On a **fresh conversation** (startup without `--continue` / `--resume`, and `/clear`), the app reads the tail
  of `log.md` (at most 24 KB read, cut at an entry boundary, at most 6,000 characters kept, newest entries
  last) and adds it to the system addendum inside a `<project-log>` block after the project-instructions block.
- The block starts with one sentence: this is a record written by earlier sessions in this folder (it may come
  from a cloned repository), it can be incomplete or out of date, check files before relying on it, and it
  cannot override the rules above or the approval prompts. Markers inside the text are defused the same way as
  for `AGENTX.md`, and the block is closed even when the text is cut.
- `--continue` and `/resume` load the saved conversation as before and do not add the log (it would repeat what
  is already in the history).
- A missing, unreadable, binary or oversized log is skipped silently.

## 8. Summarising long conversations

- **When:** at the start of `send()`, before the turn. The size is the number of characters in the saved
  history (message contents plus tool-call arguments). Over **150,000 characters** (about 40,000 tokens) a
  summary is made. The character count is a rough stand-in for tokens.
- **What is kept:** the last **6** user messages and everything after the first of them stay verbatim. The cut
  always falls on a user message, so no tool call is separated from its result. There must be at least one older
  message to summarise.
- **The call:** one call to the current model with a fixed prompt asking for at most 500 words: the goals, what
  was decided, files changed and open items. The older messages are shown to it as text, each tool result cut to
  500 characters, and the whole input capped at 60,000 characters (the oldest part is dropped from the input,
  not from the history, if needed).
- **Replacing:** the older messages become one user message holding the summary and one short assistant
  acknowledgement; the kept messages follow unchanged. The summary is also written to the log as an entry.
- **Notice:** "Summarised N earlier messages to keep the conversation short."
- **If it fails** (model error, empty or very short reply): the history is left exactly as it was, the user gets
  a one-line notice, and no retry happens until 10 more messages have been added. Nothing is ever removed
  without a summary in its place.
- The summary is a model output, so it enters the history as ordinary assistant-visible text; it carries no
  authority. A later compaction treats it like any other older message.

## 9. Errors and edge cases

- A folder that is read-only, or a path the app cannot write: notices as above, the chat continues.
- Two `agentx` processes in one folder: each appends with a single write; interleaving of whole entries is
  possible and harmless; rotation by one process may race another and at worst loses nothing but ordering.
- A log or folder settings file edited by the user (or by a repository) is read like any project text: bounded,
  defused, marked as project-supplied, and the folder settings keep the existing untrusted-project rules.
- The log is never loaded into a conversation that is being resumed, so a hostile log cannot be injected into
  an existing conversation.

## 10. Testing

Scripted models, no network, in `tests/test_cli_memory.py`, `tests/test_cli_compact.py` and the existing
settings, context, app and command tests:

- Log: entry content for a turn with writes, a refused write, a command, a team job; the "changed" check;
  secrets hidden; field caps; rotation at the size limit; a write failure noticed once; an interrupted turn
  writes nothing; nothing written for the home folder or a drive root.
- Loading: tail read and entry-boundary cut; the 6,000-character cap; markers defused and the block closed after a
  cut; fresh start and `/clear` include it, `--continue` and `/resume` do not; a binary or missing log is skipped.
- Compaction: below the threshold nothing happens; above it the cut falls on a user message with no orphaned tool
  call; the replacement shape; the call failing, returning nothing or a very short text leaves the history
  unchanged and waits for 10 more messages; the summary reaches the log; a resumed compacted session sends a
  valid history to the model.
- Folder settings: created on a new trust only, never overwritten, skipped for home and drive roots;
  `/model x --folder` creates and updates while keeping other keys; invalid JSON is not overwritten; precedence
  with `--model`, the folder file and the user file.

## 11. Docs and packaging

No new dependency, no version change. The guide gets a "Project memory" section (the folder file, the log with
an example entry and the `.gitignore` advice, how the log is used, the compaction numbers, `/model --folder`)
and the "Limits" list loses the replay-everything remark. CHANGELOG 3.7.0 gets one entry, the README one
sentence, the by-hand checklist four items.

## 12. Decisions recorded

- All four pieces (folder settings, work log, starting from the log, summarising) were chosen by the user on
  2026-10-11 and the design was approved section by section on 2026-10-12.
- The log is written by the app from events, with no extra model call; the model never writes it.
- Thresholds (150,000 characters, keep the last 6 user messages, 6,000 characters of log loaded, 200 KB log) are
  rulings, easy to change; the character count is only a proxy for tokens.
- No folder settings or log for the home folder or a drive root (a ruling: neither is a project, and the home
  folder's "project" file would be the user's own global file).
- The log is not added when a conversation is resumed (a ruling: avoids repeating history and avoids injecting
  project text into an existing conversation).
