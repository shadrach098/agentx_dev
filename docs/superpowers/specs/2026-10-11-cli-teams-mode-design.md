# Teams mode for the `agentx` command (sub-project 2 of 3)

Status: design, approved section by section on 2026-10-11. Target release: 3.7.0 (unreleased, additive; no new
dependency).
Builds on: `2026-10-10-cli-coding-assistant-design.md` (the `agentx` command, its gate and screen) and the
`Supervisor` (3.0 - 3.6.2: `stream()`, `Specialist`, `ask_user`).

## 1. Problem

`agentx` runs one `AgentRunner` that works through a request step by step in a single context. That is right
for most requests. For a big job (research several areas, change several files, then run the tests) one
context fills up, the agent loses its place, and nothing is done in parallel or by role. The framework already
has the answer, a `Supervisor` that plans the job, hands each step to a helper and combines the results, but
the terminal cannot reach it.

## 2. Goals and non-goals

Goals:

1. The normal chat agent can decide, mid-conversation, that a job is big and hand it to a team. The user
   approves the hand-off, watches the team work, and then continues the same conversation, which now contains
   what the team did.
2. `/team <task>` asks for the same thing by hand.
3. Teams add no new way to act: every helper tool call goes through the same gate, modes, deny rules,
   protected files, previews and prompts as the main chat.
4. Helpers run one at a time, so exactly one prompt is ever open and edits happen in a known order.

Non-goals (this version): helpers running in parallel, the planner inventing new helpers, memory or persistence
for team jobs, a team mode for a whole session (`--team`), `ask_when_done` / `follow_up` rounds, async
(`AsyncSupervisor`), settings for the roster.

## 3. Approaches considered

- **A (chosen): a tool the main agent can call, plus `/team`.** `start_team_job(task, why)` is one more tool in
  the main agent's list. The model decides when a job is big; the user approves each hand-off. The result
  comes back as an ordinary tool result, so it is already part of the conversation, the saved session and any
  resume. `/team <task>` is a message that tells the agent to call the tool.
- **B: a separate `/team` command only.** Simple, but the user must recognise a big job in advance, and the
  result needs hand-written plumbing into the history.
- **C: a whole-session team mode.** Every message goes through the planner. Predictable but slow and costly for
  small questions.

## 4. Structure

| Piece | Change |
|---|---|
| `agentx_dev/cli/team.py` (new) | `TEAM_TOOL_NAME`, `ROLES`, `build_helpers(...)`, `run_team_job(...)`, `format_result(...)`, `make_team_tool(...)`, `team_request(...)`. Builds the helper runners and the `Supervisor` for one job, runs it, and makes the `start_team_job` tool. |
| `approval.py` | One new tool kind, `"team"`, for `start_team_job` (section 5). |
| `preview.py` | A preview for `start_team_job` (the task, the reason, the helpers, the cost warning). |
| `tools.py` | The wrapper gets an optional `observer(name, args)`, called before the gate check. The gate still runs exactly as before. |
| `ui.py` | `Screen` draws the team events; the approval panel names the helper that is asking. |
| `app.py` | Builds the tool list (ten gated tools + `start_team_job`), tracks the current helper and the one-job-per-turn rule, wires the Supervisor's questions to the terminal, and adds the system-prompt paragraph on when to use the tool. |
| `commands.py` | `/team <task>`. |
| Docs | Guide section "Team jobs", CHANGELOG 3.7.0, README line, checklist items. |

Helpers and the `Supervisor` are built per job, from the model in use at that moment, so `/model` takes effect
on the next job.

## 5. The tool and the gate

`start_team_job(task: str, why: str = "")`. The description tells the model to use it only for large,
multi-part work (several files or areas, research plus changes plus tests) and to do small jobs itself. `task`
is capped at 4,000 characters and `why` at 500; longer text is refused with a message, not cut.

Gate rules for kind `"team"`:

| Mode | Behaviour |
|---|---|
| `plan` | Refused with the usual plan-mode message. |
| `default`, `accept-edits` | Asks. "Don't ask again" is never offered. `accept-edits` does not cover it. |
| `auto` | Runs without asking. |

Deny rules apply (`start_team_job:*` turns the feature off); the rule subject is the task text. The user's own
allow rules apply as for any non-destructive tool; project allow rules still go through the startup question.
The prompt shows the task, the reason, the three helpers and a warning that a team job makes many model calls.

A turn may start at most one team job: once a job has started, a second call in the same turn is answered with a
refusal result ("Refused: a team job already ran in this turn. ...") before the gate, so the user is not asked
again. A hand-off the user declined does not count, so it may be asked again in the same turn. The app resets the
flag when a new user message is sent.

## 6. The helpers

Three helpers, each an `AgentRunner` (ReAct, `auto_cache=False`, `parallel_tool_workers=1`, 50 steps (25 until 2026-10-13), the same
system addendum as the chat plus a short role paragraph), each with a subset of the existing **gated** tools:

| Helper | Tools | For |
|---|---|---|
| `explorer` | `read_path`, `list_directory`, `find_files`, `grep` | Looking things up. |
| `coder` | the explorer's, plus `write_file`, `edit_file` | Changing files. |
| `runner` | the explorer's, plus `run_shell`, `run_python` | Running commands and tests. |

No helper gets `delete_path`, `move_file` or `start_team_job` (so no nesting). The roles steer; they are not a
security boundary, because `run_shell` can write files. The gate is the boundary, and it is the same gate.

The `Supervisor`: `max_subtasks=6`, `max_subtask_retries=0` (a retry would repeat a helper's side effects),
`max_plan_retries=1` (planning has none), `verbose=False`, no `spawn_config`, no memory, no persistence,
`ask_when_done` off. Steps run one at a time (`Supervisor`, not `AsyncSupervisor`). Each helper starts fresh per
step; the Supervisor passes earlier results along, as it always does.

## 7. Running a job

The tool function builds the team and consumes `Supervisor.stream(task)` to the end, inside the main agent's
tool call:

- `plan` events are printed as `Team plan:` and a numbered list (`plain()`, never markup). The spinner reads
  "Planning the team job" before the plan and "Starting the team" after it.
- `dispatch` marks the helper that is now current: `-> coder: <what it was asked>`. The spinner reads
  "Running coder".
- The gated tool wrappers call the observer before the gate check, so each helper tool call prints with the
  same bullet as the main chat, prefixed with the helper's name, *before* its approval panel. The panel gets
  one extra line, `Helper: coder`.
- `subtask_result` prints one line, `done (coder): <the start of the result>` or
  `failed (coder): <the start of the error>` (the start is up to 160 characters, newlines flattened; empty shows
  "(no report)"). A skipped step counts as failed on this live line (`failed (coder): skipped: ...`) and is
  listed as "skipped" in the tool result. The spinner reads "Team is
  working" until the next dispatch.
- `question` (the planner or a helper needs a fact): asked in the terminal through the short-answer reader and
  labelled "Team question:" (the Operator does not pass its source to the asker, so the label is the same for the
  planner and the helpers). The question is cut at 12 lines and 1,500 characters before it is drawn, and its
  continuation lines are indented so it cannot pass for an approval panel. Enter, Ctrl+D (EOF) and Ctrl+C at a
  team question skip the question (the Supervisor treats it as unanswered); Ctrl+C anywhere else aborts the
  turn. The question is read with the prompt `Answer (Enter to skip):` (the same for the planner and the
  helpers). `max_questions=3`.
- `completion` ends the job and carries the result.

The tool returns one string to the main agent: "Team job finished (outcome: ...)", the numbered steps each marked
done, failed or skipped, and the final answer. The whole text (outcome line, steps and answer) is cut at
6,000 characters with a marker when longer. The main agent then carries on as with any
tool result, so the conversation, its autosave and `--continue` / `/resume` already include the job.

`/team <task>` sends the main agent the message "Call the start_team_job tool now to hand this job to the team,
then tell me what the team did: <task>". It is the same path, so the same prompt, gate and history apply. (The model may still decline; then nothing runs.)

## 8. Errors and interruption

- Ctrl+C stops the Supervisor and the helper at once and aborts the turn, as for any turn: earlier turns are
  kept, this one is not recorded, changes already made stay on disk. The Supervisor's stream is closed in a
  `finally`.
- A planner failure, a helper failure or a model error ends the job with a result string saying what happened
  ("Team job failed (<ErrorType>): <short description> Some steps may already have changed files."), or
  "Team job ended without a result." plus the same warning; a failed step inside a finished job shows in the
  numbered steps instead. The main agent decides what to do. Nothing in the job's code raises into the chat loop
  except `KeyboardInterrupt` / `SystemExit`.
- A refused tool call inside a helper is an ordinary result to that helper, as in the main chat.
- A job cannot start without a model (no key): the tool returns a short refusal ("Refused: no model is available for a team job.").

## 9. Testing

Scripted models, no network, in `tests/test_cli_team.py` and the existing app tests:

- The main model calls `start_team_job`, the Supervisor plans two steps, helpers run, and the result is in the
  conversation, the saved session file and a resumed session.
- The gate: asks in `default` and `accept-edits`, never offers "always", refuses in `plan`, runs in `auto`, a
  deny rule blocks it, a second call in one turn is refused, an over-long task is refused.
- Helpers: the explorer has no write tool, nobody has delete, move or the team tool, a helper write still hits
  the protected-file rules and asks in `default` mode, and the prompt names the helper.
- The screen: the plan, the dispatch line, a helper's tool bullet before its panel, the spinner label; text with
  escape codes and markup stays inert.
- Questions: the planner's question is asked, an empty answer skips it.
- Interruption: Ctrl+C during a job aborts the turn and leaves the session consistent.
- `/team` with and without a task.

## 10. Docs and packaging

No new dependency, no version change. The guide gets a "Team jobs" section (what it is, the three helpers, the
approval, the cost, `/team`, turning it off with a deny rule, the limits), the CHANGELOG 3.7.0 entry gets a
line, the README one sentence, and the by-hand checklist five items (ask for a big job and approve it; watch
the helper names; `/team` with and without a task; deny `start_team_job:*` and see it refused; Ctrl+C during a
job).

## 11. Decisions recorded

- Automatic escalation by the main agent, with approval each time (the user's choice, 2026-10-11), plus `/team`.
- A fixed roster of three helpers, planner-invented helpers off (the user's choice).
- One helper at a time (the user's choice).
- The result joins the conversation as the tool result (the user's choice).
- `max_subtask_retries=0` so a failed helper step is not repeated with its side effects (a ruling).
- Roles steer, the gate protects (stated so nobody reads the tool lists as a sandbox).
