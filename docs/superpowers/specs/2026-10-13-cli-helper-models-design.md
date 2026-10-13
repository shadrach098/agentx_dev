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
| `agentx_dev/cli/settings.py` | `Settings.team_models` and `Settings.team_models_source`; `load_settings` reads and validates `"teamModels"`; `write_project_team_models(cwd, names)` for `--folder`. |
| `agentx_dev/cli/team.py` | `MAX_TEAM_MODELS = 4`; `HelperModel(name, model, is_chat)`; `build_helpers(models, tools, addendum, describe_error)` builds the roles for each model; `run_team_job(..., helper_models=...)`; descriptions say which model a helper runs on. |
| `agentx_dev/cli/models.py` | `build_team_models(chat_model, names, env)` returns the usable models and one "Not used" line per skipped name. |
| `agentx_dev/cli/preview.py` | The `start_team_job` preview gets a `Helper models:` line. |
| `agentx_dev/cli/app.py` | Keeps the session's list; builds the helper models when a job starts; passes them to `run_team_job` and to the preview; shows the "Not used" lines; `/settings` shows the list. |
| `agentx_dev/cli/commands.py` | `/team-models` (show, set for the session, `--folder`, `clear`). |
| Docs | Guide "Team jobs" gets "Helper models"; CHANGELOG 3.7.0 line; README sentence; checklist items. |

## 5. The allowed list

- **Setting:** `"teamModels": ["claude-opus-5-5", "gpt-5.5"]` in the user file (`~/.agentx/settings.json`) or a
  folder's `.agentx/settings.json`; the folder file wins (the whole list, like `"model"`). A folder file is read
  only after the folder is trusted. The list names the models helpers may use *in addition to* the chat's model,
  which is always available to them.
- **Validation** (in `load_settings`, reported in the startup problems list, never fatal): a value that is not a
  list is ignored; entries that are not non-empty text are skipped; entries whose provider is unknown
  (`provider_for` is None) are skipped; duplicates are dropped (case-insensitive); more than 4 entries keep the
  first 4. The chat's model is dropped from the list when the job is built (it is already there).
- **Commands:**
  - `/team-models` shows the list in use, where it came from, and which entries are usable now (key set) and
    which are not (and why).
  - `/team-models <name> [<name> ...]` sets the list for this session only, validated as above (errors listed,
    valid names kept).
  - `/team-models <name> ... --folder` also saves it in the folder's `.agentx/settings.json`, keeping other keys,
    with the same rules as `/model --folder` (never in the home folder or a drive root, never through a link,
    never over invalid JSON).
  - `/team-models clear` empties the list for this session (helpers use the chat's model only).
- `/settings` shows the list.

## 6. Building the helpers for a job

- When a job starts, the app builds one model object per listed name with the existing `choose_model` rules, from
  the session's list at that moment (a change applies to the next job).
- A name whose API key is not set is skipped: `Not used: gpt-5.5 (no OPENAI_API_KEY)`. A name whose model cannot
  be built is skipped with the error's type only (`Not used: x (could not be set up: ValueError)`), because a
  constructor's message could echo the key. These lines are shown once, above the job's plan.
- The chat's model is always first and keeps today's helper names: `explorer`, `coder`, `runner`.
- Each other usable model adds the three roles named `explorer@<model>`, `coder@<model>`, `runner@<model>`, with
  the same tools, step limit, addendum and role text as the plain helpers. At most 4 extra models: at most 15
  helpers.
- Descriptions the planner reads: the role's description plus one sentence. Plain helpers: "Runs on
  <chat model> (the chat's model); use it unless a step clearly needs another model." Others: "The same role on
  <model>. Choose it when that model suits the step better, for example harder reasoning or a large change; its
  calls cost what that model costs."
- The planner (the `Supervisor`'s own model) is always the chat's model.

## 7. What the user sees

- The approval panel for `start_team_job` gets one line: `Helper models: claude-sonnet-5-5 (chat),
  claude-opus-5-5, gpt-5.5` (only the usable ones; computed without building the models: a listed name counts
  when its key is set).
- The plan list, the `-> coder@claude-opus-5-5: ...` dispatch line, a helper's tool bullet and the approval
  panel's `Helper:` line show the full helper name. The result the chat gets back lists each step's helper name,
  so the conversation, the saved session and the work log show which model did each step.
- Model names from settings reach the screen only through the existing `plain()` / `shown()` helpers.

## 8. Errors and edge cases

- Bad settings entries: reported once at startup, ignored; the rest works.
- Missing key or failed build: skipped with a "Not used" line; the job runs with the rest; the chat's model is
  always there, so a job never has zero helpers.
- A helper's model failing during a step (outage, rate limit, a name the provider rejects): the step fails like
  any failed step today (redacted error, marked failed, no retry since `max_subtask_retries=0`), and the job goes
  on or ends as it does now. The supervisor does not switch models on its own.
- A project file may set `teamModels` like it may set `model`; the user still sees the list in the approval panel
  before each job (outside auto mode). Helpers on another provider get the same system addendum (project
  instructions, the recent-work block, the role text): that project text is sent to that provider's API. The
  guide says so.
- The gate is unchanged: every helper call goes through the same modes, deny rules, protected files and prompts.

## 9. Testing

Scripted models, offline, in `tests/test_cli_team.py`, `tests/test_cli_settings.py`, `tests/test_cli_commands.py`
and the app tests:

- Settings: a valid list; the 4-entry cap; an unknown provider; duplicates; not a list; non-text entries; the
  folder file beating the user file; the source recorded.
- Building: no list gives exactly today's three helpers (names, descriptions unchanged except the one sentence);
  two extra models give nine helpers with the right names, models and tools; a model without a key is skipped and
  noted; a build error shows only its type; the chat's model listed again is not duplicated.
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
- The chat's model is always available to helpers and keeps today's names; with no list nothing changes (a
  ruling: a job never has zero helpers, and existing behaviour is untouched).
- The planner stays on the chat's model; at most 4 extra models (rulings, easy to change).
