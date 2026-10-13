# Helper models for `agentx` team jobs

Status: design, approved section by section on 2026-10-13. Target release: 3.7.0 (unreleased, additive; no new
dependency).
Builds on: `2026-10-11-cli-teams-mode-design.md` (team jobs, the three helpers, the gate) and
`2026-10-12-cli-project-memory-design.md` (folder settings, `/model --folder`).

## 1. Problem

A team job runs every helper (explorer, coder, runner) and the planner on the chat's model. A user chatting on
Claude Sonnet 5.5 cannot let a hard step run on Claude Opus 5.5, or on an OpenAI model, even when the key for it
is set. The framework already allows it (each helper is its own `AgentRunner` with its own model); the terminal
app never offers it.

## 2. Goals and non-goals

Goals:

1. The supervisor chooses, step by step, which model a helper runs on, from a list of models the user allows.
2. One job may mix providers (Claude and GPT helpers) when both keys are set.
3. With no list set, team jobs behave exactly as today.
4. The user sees, before approving a job, which models it may use, and sees on every step which model ran it.
5. Nothing widens what a helper may do: every helper, on any model, goes through the same gate.

Non-goals (this version): a model for the planner other than the chat's; the supervisor switching a failed step
to another model; per-role defaults fixed by the user; prices or token budgets per model; a `model` field in the
framework's plan format (approach B below); models from providers other than Anthropic and OpenAI.

## 3. Approaches considered

- **A (chosen): one helper per role and model, no framework change.** For each allowed model the app adds the
  three roles under the name `role@model`. The planner already picks a helper by name, so picking a helper picks
  a model. The Supervisor already validates names, and every display already shows the name.
- **B: a `model` field on plan steps, in the framework.** Cleaner and reusable by framework users, but it
  changes the released Supervisor's plan format, validation and both the sync and async paths. It can come later.
- **C: two fixed tiers per role ("standard", "strong") mapped to models in settings.** Small and predictable, but
  it loses the free choice among several models the user asked for.

## 4. Structure

| Piece | Change |
|---|---|
| `agentx_dev/cli/settings.py` | `Settings.team_models`, `Settings.team_models_source` and `Settings.team_model_uses`; `load_settings` reads and validates `"teamModels"` (with notes); `write_project_team_models(cwd, names, uses=None)` for `--folder` (a noted model is saved as an object). |
| `agentx_dev/cli/team.py` | `build_helpers(model, tools, addendum, describe_error, extra_models=())` builds the three roles for the chat's model and again for each extra model; `run_team_job(..., extra_models=..., chat_use=...)`; `MODEL_NOTE` and `USE_NOTE` say which model a helper runs on and what the user says it is for. |
| `agentx_dev/cli/models.py` | `MAX_TEAM_MODELS = 4`; `HelperModel(name, model, use="")` (a frozen dataclass; it lives here, next to the list helpers, so `settings.py` never imports the Supervisor); `parse_team_models(values)` returns the usable names, their notes (`MAX_USE_CHARS = 80`) and the problems, and `clean_team_models(values)` the names and problems only; `usable_team_models(chat_label, names, env)` returns the names a job would use and a "Not used" line per skipped name, without building anything; `team_models_line(chat_label, names, env)` is the approval panel's line; `build_team_models(names, env, *, chat_label, factory, uses)` returns the built models and one "Not used" line per skipped name. |
| `agentx_dev/cli/preview.py` | The `start_team_job` preview gets a `Helper models:` line (`preview_for(..., helper_models=...)`). |
| `agentx_dev/cli/app.py` | Keeps the session's list; builds the helper models when a job starts; passes them to `run_team_job` and to the preview; shows the "Not used" lines; `/settings` shows the list. |
| `agentx_dev/cli/commands.py` | `/team-models` (show, set for the session, `--folder`, `clear`). |
| Docs | Guide "Team jobs" gets "Helper models"; CHANGELOG 3.7.0 line; README sentence; checklist items. |

## 5. The allowed list

- **Setting:** `"teamModels": ["claude-opus-5-5", "gpt-5.5"]` in the user file (`~/.agentx/settings.json`) or a
  folder's `.agentx/settings.json`; the folder file wins (the whole list, like `"model"`). A folder file is read
  only after the folder is trusted. The list names the models helpers may use *in addition to* the chat's model,
  which is always available to them.
- **Validation** (`clean_team_models`, called by `load_settings` and by `/team-models`; problems are reported in
  the startup list or after the command, never fatal): a value that is not a list is ignored with a warning, and
  the list already in force (the user file's) stays; entries that are not non-empty text are skipped; a name must
  be a plain token, at most 100 characters with no spaces or control characters, or it is skipped; entries
  whose provider is unknown (`provider_for` is None: the name must start with `claude`, `gpt` or `o<digit>`) are
  skipped; repeats (any case) are dropped silently; more than 4 valid entries keep the first 4 (with a warning).
  A folder file's list replaces the user file's even when it is `[]` or has no valid entry; that turns extra
  models off for the folder. The chat's model is dropped from the list when the job is built (it is already
  there), and so are repeats of it; after `/model <name>` the next job works it out again.
- **Commands:**
  - `/team-models` shows the list in use, where it came from (a settings file, or `this session`), what a job
    may use now (the chat's model first, then the entries whose key is set) and a `Not used now: <name> (no
    <KEY_VARIABLE>)` line for each entry that cannot be used.
  - `/team-models <name> [<name> ...]` sets the list for this session only, validated as above (errors listed,
    valid names kept).
  - `/team-models <name> ... --folder` also saves it in the folder's `.agentx/settings.json`, keeping other keys,
    with the same rules as `/model --folder` (never in the home folder or a drive root, never through a link,
    never over invalid JSON).
  - `/team-models clear` empties the list for this session (helpers use the chat's model only);
    `/team-models clear --folder` also saves an empty list (`[]`) in the folder's file. `--folder` with no names
    prints the usage.
- `/settings` shows the list.

## 6. Building the helpers for a job

- When a job starts, the app builds one model object per listed name with the existing `choose_model` rules, from
  the session's list at that moment (a change applies to the next job).
- A name whose API key is not set is skipped: `Not used: gpt-5.5 (no OPENAI_API_KEY)` (the variable is
  `ANTHROPIC_API_KEY` for a Claude model). A name whose model cannot be built is skipped too. The app's model
  builder (`choose_model`) raises `ModelError` with a message written to be shown (it never contains a key; it
  wraps an unknown constructor error as "Could not set up the model (<Type>)." and passes on, for example, the
  hint to install an optional package), so for a `ModelError` the line carries that message, flattened to one
  line and cut to about 160 characters: `Not used: claude-opus-5-5 (No module named 'anthropic' - install
  agentx-dev[anthropic])`. For any other exception only the type is named (`Not used: x (could not be set up:
  <type>)`), because a constructor's message could echo the key. The app prints every line through its
  redacting `_safe`, and stops the spinner first. These lines are shown once, above the job's plan.
- Without usable extra models the helpers are today's: `explorer`, `coder`, `runner` with the roles' own
  descriptions.
- With at least one, every model, the chat's own included, gets the three roles named `explorer@<model>`,
  `coder@<model>`, `runner@<model>` (the chat's model comes first), with the same tools, step limit, addendum
  and role text. At most 4 extra models: at most 15 helpers.
- Descriptions the planner reads: the role's description, then the same words for every model so that none is
  favoured: "Runs on <model>." then, when the user gave a note, "Good for: <note>." then "There is one copy of
  each role per model: pick the copy whose model best fits this step."
- A list entry may carry a note on what the model is for: `"name=note"` or `{"model": "name", "use": "note"}`
  in settings, `name="note"` on the `/team-models` line. A note is plain text of at most 80 characters; a bad
  note is dropped with a warning and the model kept. `--folder` saves a model with a note as an object.
- The planner (the `Supervisor`'s own model) is always the chat's model.

## 7. What the user sees

- The approval panel for `start_team_job` gets one line: `Helper models: claude-sonnet-5-5 (chat),
  claude-opus-5-5, gpt-5.5` (only the usable ones; computed without building the models: a listed name counts
  when its key is set, so a model that later cannot be set up still appears there and gets a "Not used" line when
  the job starts). The chat's model is shown once; the line appears only when a list is set.
- The plan list, the `-> coder@claude-opus-5-5: ...` dispatch line, a helper's tool bullet and the approval
  panel's `Helper:` line show the full helper name. The result the chat gets back lists each step's helper name,
  so the conversation and the saved session show which model did each step. The work log keeps only its one
  `Team job: <task> (<outcome>)` line.
- Model names from settings reach the screen only through the existing `plain()` / `shown()` helpers.

## 8. Errors and edge cases

- Bad settings entries: reported once at startup, ignored; the rest works.
- Missing key or failed build: skipped with a "Not used" line; the job runs with the rest; the chat's model is
  always there, so a job never has zero helpers.
- A helper's model failing during a step (outage, rate limit, a name the provider rejects): the step fails like
  any failed step today (redacted error, marked failed, no retry since `max_subtask_retries=0`), and the job goes
  on or ends as it does now. The supervisor does not switch models on its own.
- A project file may set `teamModels` like it may set `model`. The approval panel shows the list before each job,
  but in auto mode (or with an allow rule for `start_team_job`) no panel appears, so a list that comes from the
  project's own settings file is announced once at startup, with no prompt: `This project's settings let team
  helpers also use: <names> (from <path>). /team-models clear turns that off for this session.` A list from the
  user's own file, or set in the session, and an empty or absent list, give no notice. Helpers on another
  provider get the same system addendum (project instructions, the recent-work block, the role text) and also see
  the step's task, earlier step results and everything the helper reads or runs in that step (files in `addDirs`
  folders included): all of that is sent to that provider's API. The guide says so.
- `/team-models` on an empty list says whether a settings file turned the extra models off
  (`Helper models: none - turned off by <path>.`) or none was ever set; `--folder` shows the list after the write,
  so the file is named as its source.
- The gate is unchanged: every helper call goes through the same modes, deny rules, protected files and prompts.

## 9. Testing

Scripted models, offline, in `tests/test_cli_team.py`, `tests/test_cli_settings.py`, `tests/test_cli_commands.py`
and the app tests:

- Settings: a valid list; the 4-entry cap; an unknown provider; duplicates; not a list; non-text entries; the
  folder file beating the user file; the source recorded.
- Building: no list gives exactly today's three helpers (names and descriptions unchanged);
  two extra models give nine helpers with the right names, models and tools; a model without a key is skipped and
  noted; a `ModelError` shows its (bounded, one-line) message and any other build error only its type; the
  chat's model listed again is not duplicated.
- A project's `teamModels` is announced at startup (a user-file list, a session list and no list are not); an
  empty list from a file is reported by `/team-models` as turned off by that file; a role@model name is shown
  whole in the plan, dispatch, done and tool lines.
- A whole job: a scripted planner picks `coder@<model>` and that step runs on that model's own scripted mock (and
  not on the chat's); the dispatch line and the result show the name; the helper's write still goes through the
  gate and prompts.
- Commands: `/team-models` show, set, `--folder`, `clear`, bad names; `/settings` lists the models.
- The approval panel shows the `Helper models` line.

## 10. Docs

The guide's "Team jobs" section gets a "Helper models" part (the setting, `/team-models`, the naming, the
approval line, the cost note, the provider note); the CHANGELOG 3.7.0 entry a line; the README one sentence; the
by-hand checklist three items (set two models and watch a step run on one; a model with no key is "Not used";
`/team-models clear`).

## 11. Decisions recorded

- The supervisor picks the model per step from a list the user allows (the user's choice, 2026-10-13).
- One job may mix providers when both keys are set (the user's choice).
- Approach A, no framework change (the user's choice).
- The chat's model is always available to helpers; with no list nothing changes (a ruling: a job never has zero
  helpers, and existing behaviour is untouched).
- Revised 2026-10-13 after first use: with a list, the planner ran every step on the chat's model, because the
  plain helpers were described as the default and the planner knew nothing about the others. Now every helper
  names its model with the same wording, and a model can carry a note on what it is for (the user's choice:
  "supervisor picks, no bias"). Helpers also get 50 steps instead of 25: a coder ran out of steps mid-change.
- Auto notes (2026-10-13, the user's request): a model with no note is described by the chat model in one call
  when the list is set (or at the first team job of a session for a list from settings), kept as an `auto:`
  note; the user's own notes are never asked about or replaced; `/team-models describe` asks again. Rulings:
  the chat's own model is described too (so no model is favoured), an unknown model gets no note (no
  guessing), and a failure leaves the list working without notes.
- The planner stays on the chat's model; at most 4 extra models (rulings, easy to change).
