# Helper models for `agentx` team jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a team job, the supervisor picks per step which model a helper runs on, from a list of models the user allows (`teamModels` / `/team-models`), mixing Claude and GPT when both keys are set.

**Architecture:** No framework change. For each allowed model the app registers the three roles again under `role@model` (for example `coder@claude-opus-5-5`); the planner already picks helpers by name, so it picks a model. `models.py` cleans the list and builds the extra models, `settings.py` reads and saves `teamModels`, `team.py` builds the extra helpers, the approval preview shows a `Helper models:` line, and `app.py` / `commands.py` wire the session list and `/team-models`.

**Tech Stack:** Python 3.10+, existing `agentx_dev` Supervisor/AgentRunner, `rich`, `pytest`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-13-cli-helper-models-design.md` (read it with this plan; it builds on the teams-mode and project-memory specs in the same folder).

## Global Constraints

- No new dependency. Do NOT change the version in `pyproject.toml` (stays `3.6.2`; `3.7.0` is only the CHANGELOG label). No change to the framework (`agentx_dev/Supervisor.py`, `agentx_dev/Runner/*`).
- With no `teamModels` set, team jobs behave exactly as today: three helpers named `explorer`, `coder`, `runner` on the chat's model, with today's descriptions.
- At most 4 extra models (`MAX_TEAM_MODELS = 4`), so at most 15 helpers. Extra helpers are named `<role>@<model name>`. The chat's model is always available and is never listed twice.
- The planner (the Supervisor's own model) is always the chat's model.
- A model whose key is missing or whose build fails is skipped with a line `Not used: <name> (no <KEY_VARIABLE>)` / `Not used: <name> (could not be set up: <ErrorType>)` — never the error's message (it could echo the key).
- The gate is unchanged: every helper, on any model, goes through the same modes, deny rules, protected files and prompts.
- Model names from settings or commands reach the screen only through `plain()` / `shown()` / `ChatApp._safe`.
- Tests offline with scripted models; run with `python -m pytest <file> -q` (not `py`); full suite `python -m pytest -q`.
- Plain conventional commit messages. NEVER add a `Co-Authored-By` trailer or any attribution line. Never stage anything under `.superpowers/`. Stage files by explicit path. Prefer the Edit tool over scripted edits. Do not push, tag, release or install.

## Review Focus

1. A model from the list runs a step but its calls skip the gate, or the step silently runs on the chat's model instead (Task 3 job test, Task 5 app test).
2. A missing key or a model constructor error leaks the key or crashes the job instead of "Not used" (Task 1, Task 5).
3. With no list, anything about today's team job changes (names, descriptions, planner prompt) (Task 3).
4. A hostile name in `teamModels` (markup, escape codes, 10,000 characters, not a string) breaks the screen or settings loading (Tasks 1, 2, 5).
5. `/team-models ... --folder` writes in the home folder, through a link, or over invalid JSON (Tasks 2, 5).

## File Structure

| File | Responsibility |
|---|---|
| `agentx_dev/cli/models.py` | `MAX_TEAM_MODELS`, `HelperModel`, `key_variable`, `clean_team_models`, `usable_team_models`, `team_models_line`, `build_team_models`. |
| `agentx_dev/cli/settings.py` | `Settings.team_models` / `team_models_source`; `load_settings` reads `teamModels`; `write_project_team_models` (shares one writer with `write_project_model`). |
| `agentx_dev/cli/team.py` | `build_helpers(..., extra_models=())`, `run_team_job(..., extra_models=())`. |
| `agentx_dev/cli/preview.py`, `agentx_dev/cli/ui.py` | The `Helper models:` line in the `start_team_job` approval panel. |
| `agentx_dev/cli/app.py`, `agentx_dev/cli/commands.py` | Session list, `/team-models`, `/settings` line, building the extra models at job start, `main()` wiring. |
| Tests | `tests/test_cli_helper_models.py` (new), `tests/test_cli_settings.py`, `tests/test_cli_team.py`, `tests/test_cli_preview.py`, `tests/test_cli_ui.py`, `tests/test_cli_commands.py`, `tests/test_cli_team_app.py` (append). |
| Docs | `docs/guides/cli.md`, `CHANGELOG.md`, `README.md`, `host/data.js` (regenerated). |

---

### Task 1: The allowed list and building the extra models (`models.py`)

**Files:**
- Modify: `agentx_dev/cli/models.py`
- Test: `tests/test_cli_helper_models.py` (new)

**Interfaces:**
- Consumes: `provider_for(name)`, `_key(env, variable)`, `choose_model(name, env)`, `ANTHROPIC_KEY`, `OPENAI_KEY` (all in `models.py`).
- Produces (used by Tasks 2, 3, 5):
  - `MAX_TEAM_MODELS = 4`
  - `@dataclass(frozen=True) class HelperModel: name: str; model: Any`
  - `key_variable(provider: str) -> str`
  - `clean_team_models(values: Sequence[Any]) -> Tuple[List[str], List[str]]` — (names, problems)
  - `usable_team_models(chat_label: str, names: Sequence[str], env: Mapping[str, str]) -> Tuple[List[str], List[str]]` — (usable names, "Not used" lines); builds nothing.
  - `team_models_line(chat_label: str, names: Sequence[str], env: Mapping[str, str]) -> str` — `"Helper models: <chat> (chat), a, b"`.
  - `build_team_models(names: Sequence[str], env: Mapping[str, str], *, chat_label: str = "", factory: Optional[Callable[[str, Mapping[str, str]], Any]] = None) -> Tuple[List[HelperModel], List[str]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_helper_models.py`:

```python
"""Helper models: cleaning the allowed list, which models are usable, building them."""

from agentx_dev.cli.models import (MAX_TEAM_MODELS, HelperModel, ModelError, build_team_models, clean_team_models,
                                   key_variable, team_models_line, usable_team_models)

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789", "OPENAI_API_KEY": "sk-test-0123456789abcdef"}


def test_a_clean_list_keeps_order_and_drops_repeats_silently():
    names, problems = clean_team_models(["claude-opus-5-5", "gpt-5.5", "Claude-Opus-5-5", " gpt-5.5 "])
    assert names == ["claude-opus-5-5", "gpt-5.5"] and problems == []


def test_unknown_providers_and_non_text_entries_are_skipped_with_a_problem():
    names, problems = clean_team_models(["mistral-large", 7, "", None, "o4-mini"])
    assert names == ["o4-mini"]
    assert len(problems) == 4 and any("mistral-large" in p for p in problems)


def test_more_than_the_cap_keeps_the_first_ones():
    listed = [f"claude-model-{i}" for i in range(MAX_TEAM_MODELS + 2)]
    names, problems = clean_team_models(listed)
    assert names == listed[:MAX_TEAM_MODELS] and any(f"first {MAX_TEAM_MODELS}" in p for p in problems)


def test_a_huge_name_is_cut_in_the_problem_line():
    _, problems = clean_team_models(["x" * 10_000])
    assert len(problems[0]) < 300


def test_key_variable_names_the_provider_key():
    assert key_variable("claude") == "ANTHROPIC_API_KEY" and key_variable("gpt") == "OPENAI_API_KEY"


def test_usable_models_need_a_key_and_leave_out_the_chat_model():
    usable, notes = usable_team_models("claude-sonnet-5-5", ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-5.5"],
                                       {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789"})
    assert usable == ["claude-opus-5-5"]
    assert notes == ["Not used: gpt-5.5 (no OPENAI_API_KEY)"]


def test_the_line_lists_the_chat_model_first_then_the_usable_ones():
    line = team_models_line("claude-sonnet-5-5", ["claude-opus-5-5", "gpt-5.5"], KEYS)
    assert line == "Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5, gpt-5.5"


def test_build_makes_one_model_per_usable_name_with_the_factory():
    made = []

    def factory(name, env):
        made.append(name)
        return f"MODEL<{name}>"

    built, notes = build_team_models(["claude-opus-5-5", "gpt-5.5"], KEYS, chat_label="claude-sonnet-5-5",
                                     factory=factory)
    assert built == [HelperModel("claude-opus-5-5", "MODEL<claude-opus-5-5>"), HelperModel("gpt-5.5", "MODEL<gpt-5.5>")]
    assert made == ["claude-opus-5-5", "gpt-5.5"] and notes == []


def test_a_build_error_is_skipped_and_shows_only_its_type():
    secret = KEYS["OPENAI_API_KEY"]

    def factory(name, env):
        if name.startswith("gpt"):
            raise ValueError(f"bad key {secret}")
        return "ok"

    built, notes = build_team_models(["claude-opus-5-5", "gpt-5.5"], KEYS, factory=factory)
    assert [m.name for m in built] == ["claude-opus-5-5"]
    assert notes == ["Not used: gpt-5.5 (could not be set up: ValueError)"] and secret not in " ".join(notes)


def test_a_model_error_from_the_real_factory_is_skipped_too():
    def factory(name, env):
        raise ModelError("No API key for claude")

    built, notes = build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
    assert built == [] and notes == ["Not used: claude-opus-5-5 (could not be set up: ModelError)"]


def test_a_missing_key_is_noted_and_the_factory_is_not_called():
    def factory(name, env):
        raise AssertionError("must not build without a key")

    built, notes = build_team_models(["gpt-5.5"], {}, factory=factory)
    assert built == [] and notes == ["Not used: gpt-5.5 (no OPENAI_API_KEY)"]


def test_ctrl_c_while_building_is_not_swallowed():
    import pytest

    def factory(name, env):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_helper_models.py -q`
Expected: FAIL (`ImportError: cannot import name 'MAX_TEAM_MODELS'`).

- [ ] **Step 3: Write the code**

In `agentx_dev/cli/models.py`: change the docstring's first line to `"""Choosing the chat model (a --model name, then settings, then whichever API key is set) and the extra models
team helpers may run on."""`, change the imports to

```python
import re
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple
```

add `MAX_TEAM_MODELS = 4` under `OPENAI_KEY`, and append at the end of the file:

```python
# -- the extra models team helpers may run on ------------------------------------------------------------------

@dataclass(frozen=True)
class HelperModel:
    """A model team helpers may run on; ``name`` is the label in the helper names (``coder@<name>``)."""
    name: str
    model: Any


def key_variable(provider: str) -> str:
    """The environment variable that holds the key for ``provider`` ("claude" or "gpt")."""
    return ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY


def _shown_name(name: str) -> str:
    return repr(name if len(name) <= 60 else name[:60] + "...")       # repr escapes control characters


def clean_team_models(values: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """The usable entries of a ``teamModels`` list, in order, and one problem line per entry that was dropped
    (not text, an unknown provider, past the first ``MAX_TEAM_MODELS``). Repeats, in any case, are dropped
    silently."""
    names: List[str] = []
    problems: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            problems.append("'teamModels' has an entry that is not a model name; skipped")
            continue
        name = value.strip()
        if provider_for(name) is None:
            problems.append(f"'teamModels': cannot tell which provider runs {_shown_name(name)} "
                            "(start the name with 'claude' or 'gpt'); skipped")
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    if len(names) > MAX_TEAM_MODELS:
        problems.append(f"'teamModels' lists {len(names)} models; only the first {MAX_TEAM_MODELS} are used")
        names = names[:MAX_TEAM_MODELS]
    return names, problems


def usable_team_models(chat_label: str, names: Sequence[str],
                       env: Mapping[str, str]) -> Tuple[List[str], List[str]]:
    """Which of ``names`` a team job would use now, without building anything, and a "Not used" line for each
    name whose key is not set. The chat's own model (``chat_label``) and repeats are left out silently."""
    usable: List[str] = []
    notes: List[str] = []
    seen = {str(chat_label).strip().lower()}
    for name in names:
        name = str(name).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        provider = provider_for(name)
        if provider is None:
            notes.append(f"Not used: {_shown_name(name)} (cannot tell which provider runs it)")
            continue
        variable = key_variable(provider)
        if not _key(env, variable):
            notes.append(f"Not used: {name} (no {variable})")
            continue
        usable.append(name)
    return usable[:MAX_TEAM_MODELS], notes


def team_models_line(chat_label: str, names: Sequence[str], env: Mapping[str, str]) -> str:
    """The approval panel's line: the chat's model first, then the usable extra models."""
    usable, _ = usable_team_models(chat_label, names, env)
    return "Helper models: " + ", ".join([f"{chat_label} (chat)", *usable])


def build_team_models(names: Sequence[str], env: Mapping[str, str], *, chat_label: str = "",
                      factory: Optional[Callable[[str, Mapping[str, str]], Any]] = None
                      ) -> Tuple[List[HelperModel], List[str]]:
    """Build the extra models for one team job with ``factory`` (``choose_model`` by default). A name without a
    key, or whose build raises, is skipped with a "Not used" line that names only the error's type: a
    constructor's message could echo the key. ``KeyboardInterrupt`` and ``SystemExit`` pass through."""
    build = factory or choose_model
    usable, notes = usable_team_models(chat_label, names, env)
    built: List[HelperModel] = []
    for name in usable:
        try:
            built.append(HelperModel(name, build(name, env)))
        except Exception as error:
            notes.append(f"Not used: {name} (could not be set up: {type(error).__name__})")
    return built, notes
```

- [ ] **Step 4: Run the new tests, `tests/test_cli_models.py`, then the full suite**

Run: `python -m pytest tests/test_cli_helper_models.py tests/test_cli_models.py -q` then `python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/cli/models.py tests/test_cli_helper_models.py
git commit -m "feat(cli): clean, check and build the extra models team helpers may use"
```

---

### Task 2: `teamModels` in the settings files (`settings.py`)

**Files:**
- Modify: `agentx_dev/cli/settings.py`
- Test: `tests/test_cli_settings.py` (append)

**Interfaces:**
- Consumes: `clean_team_models` (Task 1); the existing `_read`, `safe_location`, `replace_file`, `project_settings_path`, `write_project_model`.
- Produces (used by Task 5):
  - `Settings.team_models: Optional[List[str]] = None` (None: no file sets it) and `Settings.team_models_source: Optional[Path] = None`.
  - `write_project_team_models(cwd: Path, names: Sequence[str]) -> Tuple[Optional[Path], str]` — same rules as `write_project_model` (link/outside refusal, never over invalid JSON or a non-object, keeps other keys, atomic). Message `"Created|Updated <path> with helper models a, b."` or `"... with no helper models."`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_settings.py` (it has `put(path, data)`, `load_settings`, `project_settings_path`, `user_settings_path`, `json`):

```python
def test_team_models_are_read_and_cleaned(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5", "mistral-x", "gpt-5.5", "claude-opus-5-5"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["claude-opus-5-5", "gpt-5.5"]
    assert settings.team_models_source == user_settings_path(home)
    assert any("mistral-x" in p for p in settings.problems)


def test_no_team_models_key_means_none(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    settings = load_settings(folder, home)
    assert settings.team_models is None and settings.team_models_source is None


def test_the_folder_list_replaces_the_user_list(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5"]})
    put(project_settings_path(folder), {"teamModels": ["gpt-5.5"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["gpt-5.5"] and settings.team_models_source == project_settings_path(folder)


def test_an_empty_folder_list_turns_extra_models_off_for_that_folder(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5"]})
    put(project_settings_path(folder), {"teamModels": []})
    assert load_settings(folder, home).team_models == []


def test_team_models_that_are_not_a_list_are_reported_and_ignored(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": "claude-opus-5-5"})
    settings = load_settings(folder, home)
    assert settings.team_models is None and any("'teamModels' must be a list" in p for p in settings.problems)


def test_write_project_team_models_creates_and_keeps_other_keys(tmp_path):
    from agentx_dev.cli.settings import write_project_team_models
    put(project_settings_path(tmp_path), {"model": "claude-sonnet-5-5"})
    path, message = write_project_team_models(tmp_path, ["claude-opus-5-5", "gpt-5.5"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"model": "claude-sonnet-5-5", "teamModels": ["claude-opus-5-5", "gpt-5.5"]}
    assert message.startswith("Updated") and "helper models claude-opus-5-5, gpt-5.5" in message
    path, message = write_project_team_models(tmp_path, [])
    assert json.loads(path.read_text(encoding="utf-8"))["teamModels"] == [] and "no helper models" in message


def test_write_project_team_models_never_overwrites_invalid_json(tmp_path):
    from agentx_dev.cli.settings import write_project_team_models
    put(project_settings_path(tmp_path), "{not json")
    path, message = write_project_team_models(tmp_path, ["gpt-5.5"])
    assert path is None and "not valid JSON" in message
    assert project_settings_path(tmp_path).read_text(encoding="utf-8") == "{not json"


def test_write_project_model_still_says_what_it_did(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    path, message = write_project_model(tmp_path, "gpt-4o-mini")
    assert message == f"Created {path} with model gpt-4o-mini."
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_settings.py -q -k "team_models or still_says"`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'team_models'` / ImportError).

- [ ] **Step 3: Write the code**

In `agentx_dev/cli/settings.py`:

- Imports: `from typing import Any, List, Optional, Sequence, Tuple` and `from agentx_dev.cli.models import clean_team_models, provider_for`.
- Add to `Settings` (after `model_source`):

```python
    team_models: Optional[List[str]] = None                # "teamModels": None when no file sets it
    team_models_source: Optional[Path] = None              # the file the list was read from
```

- Replace `write_project_model` with a shared writer and two thin functions (keep the docstring's promises):

```python
def _write_project_key(cwd: Path, key: str, value: Any, what: str) -> Tuple[Optional[Path], str]:
    """Set ``key`` to ``value`` in ``<cwd>/.agentx/settings.json``, creating the file or keeping its other keys.
    Returns ``(path, message)``; ``path`` is None when nothing was written (``.agentx`` or the file is a link or
    leads outside the folder, the file is not valid JSON or not an object, or it could not be written), and the
    message says why. ``what`` names the change in the message. Never raises."""
    path = project_settings_path(cwd)
    if not safe_location(cwd, SETTINGS_NAME):
        return None, f"{path} is a link or points outside the folder, so it was left unchanged."
    try:
        data: dict = {}
        verb = "Created"
        if path.exists():
            verb = "Updated"
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return None, f"{path} is not valid JSON, so it was left unchanged."
            if not isinstance(loaded, dict):
                return None, f"{path} is not a JSON object, so it was left unchanged."
            data = loaded
        data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, json.dumps(data, indent=2) + "\n", prefix=".settings-")
        return path, f"{verb} {path} with {what}."
    except (OSError, ValueError, TypeError):
        return None, f"Could not write {path}."


def write_project_model(cwd: Path, model: str) -> Tuple[Optional[Path], str]:
    """Save ``model`` as this folder's model (``/model <name> --folder``). See ``_write_project_key``."""
    return _write_project_key(cwd, "model", str(model), f"model {model}")


def write_project_team_models(cwd: Path, names: Sequence[str]) -> Tuple[Optional[Path], str]:
    """Save ``names`` as this folder's ``teamModels`` (``/team-models ... --folder``). See ``_write_project_key``."""
    listed = [str(name) for name in names]
    what = ("helper models " + ", ".join(listed)) if listed else "no helper models"
    return _write_project_key(cwd, "teamModels", listed, what)
```

- In `load_settings`, inside the loop right after the `model` handling, add:

```python
        if "teamModels" in data:
            listed = data.get("teamModels")
            if not isinstance(listed, list):
                settings.problems.append(f"settings file {path}: 'teamModels' must be a list of model names; "
                                         "ignored")
            else:
                names, issues = clean_team_models(listed)
                settings.problems += [f"settings file {path}: {issue}" for issue in issues]
                settings.team_models = names               # the folder's list, read second, replaces the user's
                settings.team_models_source = path
```

- [ ] **Step 4: Run the settings tests, then the full suite**

Run: `python -m pytest tests/test_cli_settings.py -q` then `python -m pytest -q`
Expected: PASS (the existing `write_project_model` tests keep passing: same messages and rules).

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/cli/settings.py tests/test_cli_settings.py
git commit -m "feat(cli): teamModels in the settings files, saved with the folder writer"
```

---

### Task 3: Helpers on the extra models (`team.py`)

**Files:**
- Modify: `agentx_dev/cli/team.py`
- Test: `tests/test_cli_team.py` (append)

**Interfaces:**
- Consumes: `HelperModel`, `model_label` (Task 1 / existing `models.py`); existing `ROLES`, `role_prompt`, `_HelperRunner`, `HELPER_STEPS`, `Supervisor`.
- Produces (used by Task 5):
  - `build_helpers(model, tools, addendum, describe_error=str, extra_models: Sequence[HelperModel] = ())`
  - `run_team_job(task, *, model, tools, addendum, notify, ask_user=None, describe_error=str, extra_models: Sequence[HelperModel] = ())`
  - Constants `PLAIN_MODEL_NOTE` and `EXTRA_MODEL_NOTE` (format strings, see code).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_team.py` (it imports `build_helpers`, `run_team_job`, `ROLES`, `ROLE_MARK`, `router`, `plan_json`, `step`, `make_final`, `make_react_response`, `build_tools`, `Gate`, and has the `tools` fixture and `run(...)` helper):

```python
from agentx_dev.cli.models import HelperModel
from tests.conftest import MockModel


def labelled(name, script=None):
    model = MockModel(script=script or (lambda messages: make_final(f"{name} report")))
    model.model_name = name
    return model


def test_without_extra_models_the_helpers_are_exactly_todays(tools):
    helpers = build_helpers(router(), tools, "BASE PROMPT")
    assert list(helpers) == ["explorer", "coder", "runner"]
    for role in ROLES:
        assert helpers[role.name][0] == role.description


def test_each_extra_model_adds_the_three_roles_named_role_at_model(tools):
    chat = labelled("claude-sonnet-5-5")
    opus, gpt = labelled("claude-opus-5-5"), labelled("gpt-5.5")
    helpers = build_helpers(chat, tools, "BASE PROMPT",
                            extra_models=[HelperModel("claude-opus-5-5", opus), HelperModel("gpt-5.5", gpt)])
    assert list(helpers) == ["explorer", "coder", "runner",
                             "explorer@claude-opus-5-5", "coder@claude-opus-5-5", "runner@claude-opus-5-5",
                             "explorer@gpt-5.5", "coder@gpt-5.5", "runner@gpt-5.5"]
    assert helpers["coder"][1].model is chat and helpers["coder@claude-opus-5-5"][1].model is opus
    assert helpers["runner@gpt-5.5"][1].model is gpt
    for name, (description, runner) in helpers.items():
        role = name.split("@")[0]
        assert {t.name for t in runner.tools} == role_tools(role)
        assert f"{ROLE_MARK} you are the {role} helper" in runner.system_addendum
        assert "BASE PROMPT" in runner.system_addendum and runner.max_iterations == 25
    assert "Runs on claude-sonnet-5-5 (the chat's model)" in helpers["coder"][0]
    assert "The same role on claude-opus-5-5" in helpers["coder@claude-opus-5-5"][0]


def test_the_planner_picks_a_model_and_that_step_runs_on_it(tmp_path, tools):
    chat_helper_calls = []
    chat = router(plans=[plan_json(step("s1", "coder@claude-opus-5-5", "make the change"))], synth="All done.",
                  agent=lambda messages: chat_helper_calls.append(1) or make_final("chat report"))
    chat.model_name = "claude-sonnet-5-5"
    opus = labelled("claude-opus-5-5")
    result, seen = run(tmp_path, tools, chat, extra_models=[HelperModel("claude-opus-5-5", opus)])
    assert opus.calls and not chat_helper_calls                 # the step ran on opus, not on the chat's model
    assert ("dispatch", {"agent": "coder@claude-opus-5-5", "query": "make the change"}) in seen
    assert "1. coder@claude-opus-5-5 - done" in result


def test_the_planner_always_runs_on_the_chat_model(tmp_path, tools):
    chat = router(plans=[plan_json(step("s1", "explorer", "look"))], synth="Done.")
    opus = labelled("claude-opus-5-5")
    run(tmp_path, tools, chat, extra_models=[HelperModel("claude-opus-5-5", opus)])
    assert chat.planner_prompts() and not opus.calls
    assert "coder@claude-opus-5-5" in chat.planner_prompts()[0]          # the planner sees the extra helpers


def test_a_helper_on_an_extra_model_still_goes_through_the_gate(tmp_path):
    from agentx_dev.cli.approval import Answer
    asked = []
    gate = Gate(cwd=tmp_path)
    gate.ask = lambda request: asked.append(request.tool) or Answer("n", "no")
    gated = build_tools(tmp_path, [], gate)
    chat = router(plans=[plan_json(step("s1", "coder@claude-opus-5-5", "write it"))], synth="Done.")
    replies = [make_react_response("write_file", {"path": "a.txt", "content": "x", "if_exists": "refuse"}),
               make_final("declined, stopped")]
    opus = labelled("claude-opus-5-5", script=lambda messages: replies.pop(0))
    run(tmp_path, gated, chat, extra_models=[HelperModel("claude-opus-5-5", opus)])
    assert asked == ["write_file"] and not (tmp_path / "a.txt").exists()
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_team.py -q`
Expected: the new tests FAIL (`TypeError: build_helpers() got an unexpected keyword argument 'extra_models'`).

- [ ] **Step 3: Write the code**

In `agentx_dev/cli/team.py`:

- Imports: add `from agentx_dev.cli.models import HelperModel, model_label`.
- Add under `ROLE_MARK`:

```python
PLAIN_MODEL_NOTE = " Runs on {model} (the chat's model); use it unless a step clearly needs another model."
EXTRA_MODEL_NOTE = (" The same role on {model}. Choose it when that model suits the step better, for example harder "
                    "reasoning or a large change; its calls cost what that model costs.")
```

- Replace `build_helpers` with:

```python
def _helper(role: Role, model: Any, by_name: Dict[str, Any], addendum: str,
            describe_error: Callable[[BaseException], str]) -> AgentRunner:
    chosen = [by_name[name] for name in role.tools if name in by_name]
    return _HelperRunner(model=model, agent=AgentType.ReAct, tools=chosen,
                         system_addendum=(addendum or "") + role_prompt(role),
                         max_iterations=HELPER_STEPS, auto_cache=False, verbose=False,
                         parallel_tool_workers=1, describe_error=describe_error)


def build_helpers(model: Any, tools: Sequence[Any], addendum: str,
                  describe_error: Callable[[BaseException], str] = str,
                  extra_models: Sequence[HelperModel] = ()) -> Dict[str, Tuple[str, AgentRunner]]:
    """One ``AgentRunner`` per role on ``model`` (named ``explorer``, ``coder``, ``runner``), then the same roles
    on each of ``extra_models`` (named ``<role>@<model name>``). Each holds only its slice of ``tools`` (the
    already gated tools, picked by name), so every helper's calls go through the same gate as the main chat.
    ``addendum`` is the chat's own system addendum; the role text follows it. Without extra models the
    descriptions are the roles' own; with them, each description says which model the helper runs on. An error
    a helper's run raises reaches the Supervisor as ``describe_error(error)`` only."""
    by_name = {tool.name: tool for tool in tools}
    extras = list(extra_models)
    chat_note = PLAIN_MODEL_NOTE.format(model=model_label(model)) if extras else ""
    helpers: Dict[str, Tuple[str, AgentRunner]] = {}
    for role in ROLES:
        helpers[role.name] = (role.description + chat_note, _helper(role, model, by_name, addendum, describe_error))
    for extra in extras:
        for role in ROLES:
            helpers[f"{role.name}@{extra.name}"] = (role.description + EXTRA_MODEL_NOTE.format(model=extra.name),
                                                    _helper(role, extra.model, by_name, addendum, describe_error))
    return helpers
```

- In `run_team_job`: add the keyword parameter `extra_models: Sequence[HelperModel] = ()` (after `describe_error`), mention it in the docstring ("``extra_models`` adds the roles on other models; the planner stays on ``model``"), and change the build line to
  `helpers = build_helpers(model, tools, addendum, describe_error, extra_models=extra_models)`.

- [ ] **Step 4: Run the team tests, then the full suite**

Run: `python -m pytest tests/test_cli_team.py tests/test_cli_team_app.py -q` then `python -m pytest -q`
Expected: PASS. If an existing test monkeypatches `build_helpers` with a 4-argument fake, give the fake `**kw` (only the fake, never the assertion), and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/cli/team.py tests/test_cli_team.py
git commit -m "feat(cli): team helpers on extra models, named role@model, picked by the planner"
```

---

### Task 4: The `Helper models:` line in the approval panel (`preview.py`, `ui.py`)

**Files:**
- Modify: `agentx_dev/cli/preview.py`, `agentx_dev/cli/ui.py`
- Test: `tests/test_cli_preview.py`, `tests/test_cli_ui.py` (append)

**Interfaces:**
- Consumes: `team_models_line` output (a plain string, Task 1) — supplied by the app in Task 5.
- Produces (used by Task 5):
  - `preview_for(tool, args, cwd, helper_models: str = "") -> Preview` — for `start_team_job` the line is added after `TEAM_HELPERS_LINE`; ignored for other tools.
  - `make_asker(console, read_line, cwd, select=None, helper=None, helper_models: Optional[Callable[[], str]] = None)` — calls `helper_models()` only for `start_team_job`; any error or a non-string gives no line.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_preview.py`:

```python
def test_the_team_preview_shows_the_helper_models_line(tmp_path):
    p = preview_for("start_team_job", {"task": "Survey", "why": ""}, tmp_path,
                    helper_models="Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5")
    assert "Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5" in p.body


def test_without_a_line_the_team_preview_is_unchanged(tmp_path):
    assert "Helper models" not in preview_for("start_team_job", {"task": "Survey"}, tmp_path).body


def test_other_tools_ignore_the_helper_models_line(tmp_path):
    p = preview_for("run_shell", {"command": "ls"}, tmp_path, helper_models="Helper models: x")
    assert "Helper models" not in p.body
```

Append to `tests/test_cli_ui.py` (it has `console()`, `lines(...)`, `Request`, `make_asker`):

```python
def team_request_for_ui():
    return Request(tool="start_team_job", kind="team", subject="Survey", args={"task": "Survey", "why": ""},
                   remember=False, grant="")


class TestHelperModelsLine:
    def test_the_team_panel_shows_the_line(self, tmp_path):
        c, out = console()
        make_asker(c, lines("y"), tmp_path,
                   helper_models=lambda: "Helper models: claude-sonnet-5-5 (chat), gpt-5.5")(team_request_for_ui())
        assert "Helper models: claude-sonnet-5-5 (chat), gpt-5.5" in out.getvalue()

    def test_markup_in_a_model_name_is_printed_literally(self, tmp_path):
        c, out = console()
        make_asker(c, lines("y"), tmp_path,
                   helper_models=lambda: "Helper models: x (chat), [red]evil[/red]\x1b[2J")(team_request_for_ui())
        assert "[red]evil[/red]" in out.getvalue() and "\x1b" not in out.getvalue()

    def test_a_failing_line_source_gives_no_line_and_still_asks(self, tmp_path):
        c, out = console()

        def broken():
            raise RuntimeError("boom")

        answer = make_asker(c, lines("y"), tmp_path, helper_models=broken)(team_request_for_ui())
        assert answer.choice == "y" and "Helper models" not in out.getvalue()

    def test_other_tools_never_call_the_line_source(self, tmp_path):
        c, _ = console()
        called = []
        make_asker(c, lines("y"), tmp_path, helper_models=lambda: called.append(1) or "x")(edit_request())
        assert called == []
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_preview.py tests/test_cli_ui.py -q -k "helper_models or HelperModels"`
Expected: FAIL (`unexpected keyword argument 'helper_models'`).

- [ ] **Step 3: Write the code**

`agentx_dev/cli/preview.py`:

```python
def _team_job(args: Mapping[str, Any], helper_models: str = "") -> Preview:
    task = str(args.get("task") or "").strip()
    why = str(args.get("why") or "").strip()
    lines = [f"Task: {task}"]
    if why:
        lines.append(f"Why a team: {why}")
    lines += ["", TEAM_HELPERS_LINE]
    if helper_models:
        lines.append(helper_models)
    lines.append("A team job makes many model calls and can take several minutes. Every edit or command a "
                 "helper tries is asked about as usual.")
    body, truncated = _whole("\n".join(lines))
    return Preview("Start a team job", body, "text", truncated, COMMAND_TOO_LONG if truncated else "")
```

Change `_build(tool, args, cwd)` to `_build(tool, args, cwd, helper_models: str = "")` with `return _team_job(args, helper_models)` for `start_team_job`, and `preview_for` to:

```python
def preview_for(tool: str, args: Mapping[str, Any], cwd: Path, helper_models: str = "") -> Preview:
    """What to show the user before ``tool`` runs with ``args`` in ``cwd``. ``helper_models`` (the models a team
    job may use) is shown in a ``start_team_job`` preview only. Never raises."""
    try:
        return _build(tool, args, cwd, helper_models)
    except Exception:
        return _generic(tool, args)
```

`agentx_dev/cli/ui.py` — `make_asker` gains `helper_models: Optional[Callable[[], str]] = None` (last parameter; add one docstring sentence: "``helper_models`` (optional) returns the ``Helper models:`` line for a ``start_team_job`` prompt."), and its first lines become:

```python
    def ask(request: Request) -> Answer:
        line = ""
        if helper_models is not None and request.tool == "start_team_job":
            try:
                value = helper_models()
                line = plain(value) if isinstance(value, str) else ""
            except Exception:                         # a broken source must never stop the question
                line = ""
        preview = preview_for(request.tool, request.args, cwd, helper_models=line)
```

(`plain` is already defined in `ui.py`; check that it strips control characters and that the preview body is printed as rich `Text`, so markup stays literal. If the body is printed some other way, keep the test as the contract.)

- [ ] **Step 4: Run the preview and UI tests, then the full suite**

Run: `python -m pytest tests/test_cli_preview.py tests/test_cli_ui.py -q` then `python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/cli/preview.py agentx_dev/cli/ui.py tests/test_cli_preview.py tests/test_cli_ui.py
git commit -m "feat(cli): the team approval panel names the models helpers may use"
```

---

### Task 5: Wiring: the session list, `/team-models`, `/settings`, the job (`app.py`, `commands.py`)

**Files:**
- Modify: `agentx_dev/cli/app.py`, `agentx_dev/cli/commands.py`
- Test: `tests/test_cli_commands.py`, `tests/test_cli_team_app.py` (append)

**Interfaces:**
- Consumes: Tasks 1 - 4 (`clean_team_models`, `usable_team_models`, `team_models_line`, `build_team_models`, `Settings.team_models`, `Settings.team_models_source`, `write_project_team_models`, `run_team_job(..., extra_models=)`, `make_asker(..., helper_models=)`); existing `ChatApp` (`_safe`, `notice`, `error`, `model_label`, `model_factory`, `env`, `_memory_on`, `_start_team_job`, `show_settings`), `tests.test_cli_app.make_app / Lines`, `tests.test_cli_team_app.team_model / CALL`.
- Produces:
  - `ChatApp(..., team_models: Optional[Sequence[str]] = None, team_models_source: Optional[Path] = None)`; attributes `team_models: List[str]`, `team_models_source: Optional[Path]`.
  - `ChatApp.helper_models_line() -> str` ("" when the list is empty or there is no model).
  - `ChatApp.show_team_models() -> None`, `ChatApp.set_team_models(names: Sequence[str], folder: bool = False) -> None`.
  - `/team-models` in `commands.py`.

- [ ] **Step 1: Write the failing command tests**

Append to `tests/test_cli_commands.py`:

```python
def test_team_models_without_arguments_shows_the_list():
    ctx = Ctx()
    ctx.show_team_models = lambda: ctx.calls.append(("show-team",))
    run_command(ctx, "/team-models")
    assert ctx.calls == [("show-team",)]


def test_team_models_with_names_sets_them_for_the_session_or_the_folder():
    ctx = Ctx()
    ctx.set_team_models = lambda names, folder=False: ctx.calls.append(("set-team", list(names), folder))
    run_command(ctx, "/team-models claude-opus-5-5 gpt-5.5")
    run_command(ctx, "/team-models gpt-5.5 --folder")
    run_command(ctx, "/team-models clear")
    assert ctx.calls == [("set-team", ["claude-opus-5-5", "gpt-5.5"], False), ("set-team", ["gpt-5.5"], True),
                         ("set-team", [], False)]


def test_team_models_folder_needs_names_or_clear():
    ctx = Ctx()
    run_command(ctx, "/team-models --folder")
    assert "Usage: /team-models" in ctx.errors[-1]


def test_team_models_is_listed_in_help():
    from agentx_dev.cli.commands import command_names
    assert "/team-models" in command_names()
```

- [ ] **Step 2: Write the failing app tests**

Append to `tests/test_cli_team_app.py`:

```python
from tests.conftest import MockModel as _Mock

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789", "OPENAI_API_KEY": "sk-test-0123456789abcdef"}


def extra(name, reply="opus report"):
    model = _Mock(script=lambda messages: make_final(reply))
    model.model_name = name
    return model


def factory_for(chat, **extras):
    def factory(name, env):
        return extras[name] if name in extras else chat
    return factory


def test_a_step_runs_on_the_model_the_planner_picked(tmp_path):
    chat = team_model([step("s1", "coder@claude-opus-5-5", "make the change")])
    chat.model_name = "claude-sonnet-5-5"
    opus = extra("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory_for(chat, **{"claude-opus-5-5": opus}),
                              team_models=["claude-opus-5-5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5" in text        # in the approval panel
    assert "-> coder@claude-opus-5-5: make the change" in text
    assert opus.calls and chat.state["helper_calls"] == 0
    assert "coder@claude-opus-5-5 - done" in history_text(app)


def test_a_model_without_a_key_is_not_used_and_the_job_still_runs(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"),
                              env={"ANTHROPIC_API_KEY": KEYS["ANTHROPIC_API_KEY"]},
                              team_models=["gpt-5.5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: gpt-5.5 (no OPENAI_API_KEY)" in text and "done (explorer)" in text


def test_a_model_that_cannot_be_built_shows_only_the_error_type(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-5-5"

    def factory(name, env):
        if name == "gpt-5.5":
            raise ValueError(f"bad {KEYS['OPENAI_API_KEY']}")
        return chat

    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory, team_models=["gpt-5.5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: gpt-5.5 (could not be set up: ValueError)" in text and KEYS["OPENAI_API_KEY"] not in text


def test_without_a_list_the_team_job_is_unchanged(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"))
    assert app.start() == 0
    assert "Helper models" not in out.getvalue() and "Not used" not in out.getvalue()


def test_team_models_command_sets_shows_and_clears_for_the_session(tmp_path):
    chat = MockModel(script=[])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 mistral-x gpt-5.5", "/team-models",
                                              "/team-models clear", "/exit"), chat,
                              env={"ANTHROPIC_API_KEY": KEYS["ANTHROPIC_API_KEY"]})
    assert app.start() == 0
    text = out.getvalue()
    assert "mistral-x" in text                                        # the bad name is reported
    assert "claude-opus-5-5" in text and "Not used now: gpt-5.5 (no OPENAI_API_KEY)" in text
    assert app.team_models == []


def test_team_models_folder_saves_them_in_the_folder_file(tmp_path):
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["teamModels"] == ["claude-opus-5-5"] and app.team_models == ["claude-opus-5-5"]


def test_team_models_folder_is_refused_in_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "is_project_folder", lambda cwd, home: False)
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert not (cwd / ".agentx" / "settings.json").exists() and "home folder" in out.getvalue()


def test_settings_shows_the_helper_models(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/settings", "/exit"), chat, env=KEYS,
                              team_models=["claude-opus-5-5"])
    assert app.start() == 0
    assert "Helper models: claude-opus-5-5" in out.getvalue()


def test_a_hostile_model_name_is_printed_inert(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/team-models claude-[red]x[/red]\x1b[2J", "/team-models", "/exit"),
                              chat, env=KEYS)
    assert app.start() == 0
    assert "\x1b" not in out.getvalue()
```

- [ ] **Step 3: Run them to see them fail**

Run: `python -m pytest tests/test_cli_commands.py tests/test_cli_team_app.py -q`
Expected: FAIL (`unexpected keyword argument 'team_models'`, unknown command `/team-models`).

- [ ] **Step 4: `commands.py`**

Add before `COMMANDS`:

```python
TEAM_MODELS_USAGE = "Usage: /team-models <name> [<name> ...] [--folder], or /team-models clear [--folder]"


def _team_models(ctx: Any, arg: str) -> None:
    words = arg.split()
    folder = "--folder" in words
    names = [word for word in words if word != "--folder"]
    if not names:
        if folder:
            ctx.error(TEAM_MODELS_USAGE)
            return
        ctx.show_team_models()
        return
    if [name.lower() for name in names] == ["clear"]:
        names = []
    ctx.set_team_models(names, folder=folder)
```

and add to `COMMANDS` after `team`:
`Command("team-models", "Models team helpers may also use: /team-models [names|clear] [--folder]", _team_models),`

- [ ] **Step 5: `app.py`**

Imports: extend the models import with `build_team_models, clean_team_models, team_models_line, usable_team_models`; extend the settings import with `write_project_team_models`.

`ChatApp.__init__`: add the keyword parameters `team_models: Optional[Sequence[str]] = None, team_models_source: Optional[Path] = None` (after `model_source`), and set

```python
        self.team_models: List[str] = list(team_models or [])
        self.team_models_source = team_models_source if team_models else None   # the file the list came from
```

and pass `helper_models=self.helper_models_line` to the `make_asker(...)` call.

Add next to `save_folder_model`:

```python
    def helper_models_line(self) -> str:
        """The approval panel's ``Helper models:`` line; "" when no extra models are set or there is no model."""
        if not self.team_models or self.model is None:
            return ""
        return team_models_line(self.model_label, self.team_models, self.env)

    def show_team_models(self) -> None:
        """``/team-models``: the list, where it came from, and which entries a job would use now."""
        if not self.team_models:
            self.notice("Helper models: none set, so team helpers use the chat's model "
                        f"({self._safe(self.model_label, 80)}). Set some with /team-models <name> [<name> ...]; "
                        "add --folder to keep them for this folder.")
            return
        usable, notes = usable_team_models(self.model_label, self.team_models, self.env)
        source = (f" (from {self._safe(self.team_models_source, 300)})" if self.team_models_source
                  else " (this session)")
        lines = [f"Helper models{source}: {self._safe(', '.join(self.team_models), 400)}",
                 f"A team job may use: {self._safe(self.model_label, 80)} (chat)"
                 + "".join(f", {self._safe(name, 80)}" for name in usable)]
        lines += [self._safe(note.replace('Not used:', 'Not used now:', 1), 200) for note in notes]
        lines.append("Change them with /team-models <name> ..., /team-models clear, or add --folder to save.")
        self.notice("\n".join(lines))

    def set_team_models(self, names: Sequence[str], folder: bool = False) -> None:
        """``/team-models <names>`` (session) or ``... --folder`` (also saved in this folder's settings file)."""
        cleaned, problems = clean_team_models(list(names))
        for problem in problems:
            self.error(self._safe(problem, 300))
        if names and not cleaned:
            self.error("No usable model name was given, so the list was left unchanged.")
            return
        self.team_models = cleaned
        self.team_models_source = None
        self.show_team_models()
        if not folder:
            return
        if not self._memory_on:
            self.notice("No folder settings file is kept for your home folder or a drive root.")
            return
        path, message = write_project_team_models(self.cwd, cleaned)
        (self.notice if path is not None else self.error)(self._safe(message, 600))
        if path is not None:
            self.team_models_source = path
```

`_start_team_job`: build the extra models before planning and pass them on:

```python
        self._team_used = True
        extras, notes = build_team_models(self.team_models, self.env, chat_label=self.model_label,
                                          factory=self.model_factory)
        for note in notes:
            self.notice(self._safe(note, 200))
        self.progress.start("Planning the team job")
        try:
            return run_team_job(task, model=self.model, tools=self.base_tools, addendum=self.addendum,
                                notify=self._team_notify, ask_user=self._team_question,
                                describe_error=self._safe, extra_models=extras)
```

`show_settings`: add a line after `Extra folders:`:
`f"Helper models: {self._safe(', '.join(self.team_models), 400) or 'none (helpers use the chat model)'}\n"`
and add to the closing sentence: `"/team-models sets the models team helpers may also use. "`.

`main()`: pass `team_models=settings.team_models, team_models_source=settings.team_models_source` to `ChatApp(...)`.

- [ ] **Step 6: Run the new tests and fix what they show**

Run: `python -m pytest tests/test_cli_commands.py tests/test_cli_team_app.py -q`
Expected: PASS. Likely adjustments, most probable first:
- `make_app` passes extra keywords straight to `ChatApp`, so `team_models=` works once `__init__` accepts it.
- `team_model(...)` is one MockModel for the chat, planner and plain helpers; giving it `model_name` makes `model_label` print that name. If `MockModel` already defines `model_name`, set it the same way the existing tests do.
- If the approval panel prints the preview body through something other than `Text`, keep the markup test as the contract and fix the printing, not the test.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agentx_dev/cli/app.py agentx_dev/cli/commands.py tests/test_cli_commands.py tests/test_cli_team_app.py
git commit -m "feat(cli): /team-models and helper models in team jobs"
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/guides/cli.md`, `CHANGELOG.md`, `README.md`, `host/data.js` (regenerated); the spec only where the code differs.

**Interfaces:**
- Consumes: the behaviour of Tasks 1 - 5 (read the code, not only this plan, and copy user-visible messages exactly).
- Produces: documentation only.

- [ ] **Step 1: The guide**

In `docs/guides/cli.md`, inside the "Team jobs" section (read it first and match its style), add a "Helper models" part covering:
1. What it is: the supervisor picks, step by step, which model a helper runs on, from models you allow; the chat's model is always available; one job may mix Claude and GPT when both keys are set; with no list nothing changes.
2. Setting the list: `"teamModels": ["claude-opus-5-5", "gpt-5.5"]` in `~/.agentx/settings.json` or a folder's `.agentx/settings.json` (the folder's list replaces yours; `[]` turns extra models off there); at most 4; names must start with `claude` or `gpt`/`o<digit>`; bad entries are listed at startup.
3. `/team-models` (show), `/team-models <names>` (session), `--folder` (save), `/team-models clear`; `/settings` shows the list.
4. What you see: the approval panel's `Helper models:` line; helper names `coder@claude-opus-5-5` in the plan, the dispatch line, the `Helper:` line and the result; `Not used: ...` lines when a key is missing or a model cannot be set up.
5. Cost and data: each step costs what its model costs; helpers on another provider receive the same system prompt as the chat (project instructions, the recent-work block), so that text goes to that provider's API; the gate and prompts are the same for every helper; the planner always uses the chat's model.

Add three by-hand checklist items (continue the numbering): set two models with `/team-models` and ask for a big job — see the `Helper models` line and a step dispatched to `coder@<model>`; list a model whose key is not set — see `Not used: ... (no ...)`; `/team-models clear` — the next job shows no `Helper models` line.

- [ ] **Step 2: The other docs**

- `CHANGELOG.md`, under `## [3.7.0] - Unreleased` / `### Added`, in the `agentx` list: "Helper models for team jobs: allow extra models with `teamModels` or `/team-models`, and the supervisor picks per step which model a helper runs on (Claude and GPT can be mixed when both keys are set)."
- `README.md`, in "The agentx command": one sentence that team helpers can run on other models you allow.
- The spec: correct only where the code differs (exact messages, names; note that `HelperModel` and the list helpers live in `models.py`).

- [ ] **Step 3: Regenerate, test, commit**

Run: `python host/build_data.py` then `python -m pytest -q`
Expected: PASS.

```bash
git add docs/guides/cli.md CHANGELOG.md README.md docs/superpowers/specs/2026-10-13-cli-helper-models-design.md host/data.js
git commit -m "docs: helper models for team jobs in the agentx guide, README and changelog"
```

---

## Self-review (done while writing this plan)

- **Spec coverage:** §5 allowed list = Task 1 (`clean_team_models`, cap, providers) + Task 2 (settings, folder replaces user, `--folder` writer) + Task 5 (`/team-models` show/set/`--folder`/`clear`, `/settings`); §6 building = Task 1 (`build_team_models`, "Not used" lines, type-only errors) + Task 3 (names, descriptions, planner on the chat model) + Task 5 (built at job start from the session list); §7 what the user sees = Task 4 (approval line) + Task 3/5 (names in plan, dispatch, result); §8 errors = Tasks 1, 3, 5 tests; §9 testing = the tests listed; §10 docs = Task 6.
- **Spec deviation recorded:** `HelperModel` lives in `models.py` (not `team.py`), next to the list helpers, so `settings.py` never imports the Supervisor. Task 6 corrects the spec's structure table.
- **Ruling:** the "Runs on <chat model>" sentence is added to the plain helpers only when extra models exist, so with no list the planner sees exactly today's descriptions (spec goal 3 wins over §9's "unchanged except the one sentence").
- **Placeholder scan:** none.
- **Type consistency:** `HelperModel(name, model)`, `clean_team_models(values) -> (names, problems)`, `usable_team_models(chat_label, names, env) -> (usable, notes)`, `team_models_line(chat_label, names, env) -> str`, `build_team_models(names, env, *, chat_label, factory) -> (built, notes)` (Task 1) match their uses in Tasks 2, 3, 5; `build_helpers(..., extra_models=)` / `run_team_job(..., extra_models=)` (Task 3) match Task 5; `preview_for(..., helper_models=)` / `make_asker(..., helper_models=)` (Task 4) match Task 5; `write_project_team_models(cwd, names)` (Task 2) matches Task 5.
