# Teams mode for `agentx` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The normal `agentx` chat agent can hand a big job to a `Supervisor` with three helpers (explorer, coder, runner) through a new `start_team_job` tool, after the user approves; `/team <task>` asks for the same thing by hand.

**Architecture:** `start_team_job` is one more gated tool in the main agent's list. Its function builds three `AgentRunner` helpers from the existing gated tools (subset by name) and a `Supervisor`, consumes `Supervisor.stream()` to the end inside the main agent's tool call, and returns one result string, which is already part of the conversation as an ordinary tool result. Helpers run one at a time. Every helper tool call goes through the same `Gate`, modes, deny rules, previews and prompts as the main chat.

**Tech Stack:** Python 3.10+ (the package's floor), `agentx_dev.Supervisor`, `AgentRunner`, `rich`, `prompt_toolkit` (already dependencies), `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-11-cli-teams-mode-design.md` (read it with this plan; it also builds on `2026-10-10-cli-coding-assistant-design.md`).

## Global Constraints

- No new dependency. Do NOT change the version in `pyproject.toml` (stays `3.6.2`; `3.7.0` is only the CHANGELOG label).
- Everything printed that came from the model, the project or a saved session goes through `plain()` / rich `Text`, never markup (`agentx_dev/cli/ui.py`).
- The gate is the only boundary. Helper tool lists steer; they are not a sandbox. Helpers never get `delete_path`, `move_file` or `start_team_job`.
- A turn may start at most one team job. Steps run one at a time (`Supervisor`, not `AsyncSupervisor`), `max_subtasks=6`, `max_subtask_retries=0`, `max_plan_retries=1`, `verbose=False`, no `spawn_config`, no memory, no persistence, `ask_when_done` off.
- Tests run offline with scripted models. Run tests with `python -m pytest <file> -q` (not `py`). The full suite is `python -m pytest -q`.
- Plain conventional commit messages. NEVER add a `Co-Authored-By` trailer or any attribution line. Never stage anything under `.superpowers/`. Stage files by explicit path.
- Prefer the Edit tool over scripted edits (backslash escapes get mangled). No code in this plan needs a backslash.
- Do not push, tag, release or install anything.

## Review Focus

The five failure modes the spec implies that are most likely to bite; each has a test in the task that owns the code.

1. A model that calls `start_team_job` twice in one turn: the second call must be refused with a clear result, not run a second team (Task 4).
2. A helper's tool call that the gate protects (a write to `.git/config`, any write in `default` mode): it must still be refused or asked about, with the helper's name on the prompt (Task 4).
3. Ctrl+C in the middle of a job: the turn aborts, the helper state is reset, the next message works (Task 4).
4. Task, reason, plan or helper text containing rich markup or escape codes: it must print inert (Task 3).
5. An empty or over-long `task` (more than 4,000 characters), and a Supervisor that raises, returns nothing or is interrupted: a short failure result, never a traceback in the chat loop (Task 2).

## File Structure

| File | Responsibility |
|---|---|
| `agentx_dev/cli/team.py` (new) | The roster (`ROLES`), `build_helpers`, `run_team_job`, `format_result`, the tool schema and `make_team_tool`, `team_request`. No terminal I/O: it reports through a `notify` callback. |
| `agentx_dev/cli/approval.py` | `start_team_job` is a new tool kind, `"team"`. |
| `agentx_dev/cli/preview.py` | A preview for `start_team_job`. |
| `agentx_dev/cli/tools.py` | `gate_tool` / `build_tools` get an optional `observer(name, args)` called before the gate check. |
| `agentx_dev/cli/ui.py` | `Screen` draws team events and helper tool calls; the approval panel names the helper. |
| `agentx_dev/cli/app.py` | Adds the team tool to the tool list, the per-turn flag, the helper/progress bookkeeping, the Supervisor's question prompt, `run_team_command`. |
| `agentx_dev/cli/commands.py` | `/team <task>`. |
| `agentx_dev/cli/context.py` | One bullet in `BASE_PROMPT` telling the model when to use the tool. |
| Tests | `tests/test_cli_approval.py`, `test_cli_preview.py`, `test_cli_tools.py`, `test_cli_ui.py`, `test_cli_commands.py` (append), `tests/test_cli_team.py` and `tests/test_cli_team_app.py` (new). |
| Docs | `docs/guides/cli.md`, `CHANGELOG.md`, `README.md`, the spec (small corrections), `host/data.js` (regenerated). |

---

### Task 1: The `team` tool kind in the gate, and its preview

**Files:**
- Modify: `agentx_dev/cli/approval.py` (the `TOOL_KINDS` dict near line 16, `kind_of`'s docstring, the `auto` branch in `Gate.check`)
- Modify: `agentx_dev/cli/preview.py` (`_build`, plus a new `_team_job`)
- Test: `tests/test_cli_approval.py` (append), `tests/test_cli_preview.py` (append)

**Interfaces:**
- Consumes: `Gate(mode, allow, deny, ask, cwd)`, `Answer`, `Request`, `PLAN_REFUSAL`, `preview_for(tool, args, cwd) -> Preview`.
- Produces: `kind_of("start_team_job") == "team"`; the gate asks for it in `default` and `accept-edits`, with `Request.remember == False` and `Request.grant == ""`; refuses it in `plan`; runs it in `auto`; obeys deny rules and the user's allow rules. `preview_for("start_team_job", {"task", "why"}, cwd)` returns `Preview(title="Start a team job", kind="text")`.

- [ ] **Step 1: Write the failing gate tests**

Append to `tests/test_cli_approval.py`:

```python
class TestTeamJobTool:
    ARGS = {"task": "Survey the project and report", "why": "it spans many areas"}

    def gate(self, mode="default", answer="y", **kw):
        asked = []

        def ask(request):
            asked.append(request)
            return Answer(answer)

        return Gate(mode=mode, ask=ask, cwd=Path.cwd(), **kw), asked

    def test_it_is_a_team_kind(self):
        assert kind_of("start_team_job") == "team"

    @pytest.mark.parametrize("mode", ["default", "accept-edits"])
    def test_it_asks_and_never_offers_always(self, mode):
        gate, asked = self.gate(mode)
        assert gate.check("start_team_job", self.ARGS) is None
        assert len(asked) == 1
        assert asked[0].kind == "team" and asked[0].remember is False and asked[0].grant == ""
        assert asked[0].subject == self.ARGS["task"]

    def test_plan_mode_refuses_it_without_asking(self):
        gate, asked = self.gate("plan")
        assert gate.check("start_team_job", self.ARGS) == PLAN_REFUSAL
        assert asked == []

    def test_auto_mode_runs_it_without_asking(self):
        gate, asked = self.gate("auto")
        assert gate.check("start_team_job", self.ARGS) is None
        assert asked == []

    def test_a_deny_rule_turns_the_feature_off_in_every_mode(self):
        for mode in ("default", "accept-edits", "auto"):
            gate, asked = self.gate(mode, deny=["start_team_job:*"])
            assert "deny rule" in gate.check("start_team_job", self.ARGS)
            assert asked == []

    def test_the_users_own_allow_rule_lets_it_run_in_default_mode(self):
        gate, asked = self.gate("default", allow=["start_team_job"])
        assert gate.check("start_team_job", self.ARGS) is None
        assert asked == []

    def test_answering_always_remembers_nothing(self):
        gate, asked = self.gate("default", answer="a")
        assert gate.check("start_team_job", self.ARGS) is None
        assert not gate.grants and gate.mode == "default"
        assert gate.check("start_team_job", self.ARGS) is None
        assert len(asked) == 2                                   # asked again: "a" is not a grant here
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_approval.py -q -k TestTeamJobTool`
Expected: FAIL (`kind_of("start_team_job")` is `"command"`; the auto test asks or the plan/deny behaviour differs).

- [ ] **Step 3: Write the gate change**

In `agentx_dev/cli/approval.py`, add the kind to `TOOL_KINDS`:

```python
TOOL_KINDS = {
    "read_path": "read", "list_directory": "read", "find_files": "read", "grep": "read",
    "write_file": "edit", "edit_file": "edit",
    "run_shell": "command", "run_python": "command",
    "delete_path": "destructive", "move_file": "destructive",
    "start_team_job": "team",
}
```

Change `kind_of`'s docstring to `"""read / edit / command / destructive / team. A tool the gate does not know counts as a command."""`.

In `Gate.check`, change the `auto` branch (currently `kind in ("edit", "command")`) to include the team kind, and its comment:

```python
        if self._mode == "auto" and tool in TOOL_KINDS and kind in ("edit", "command", "team"):
            return None             # auto: only delete / move and tools the gate does not know still ask
```

Nothing else changes: a `team` call is not compound (it has no `command` argument), `_grant_for` already returns `None` for it, so no "always" is offered or remembered, and `accept-edits` only covers `kind == "edit"`.

- [ ] **Step 4: Run the gate tests**

Run: `python -m pytest tests/test_cli_approval.py -q`
Expected: PASS (all earlier tests too).

- [ ] **Step 5: Write the failing preview tests**

Append to `tests/test_cli_preview.py`:

```python
def test_a_team_job_preview_names_the_task_the_reason_and_the_helpers(tmp_path):
    preview = preview_for("start_team_job", {"task": "Survey the project", "why": "many areas"}, tmp_path)
    assert preview.title == "Start a team job" and preview.kind == "text" and not preview.truncated
    for text in ("Survey the project", "many areas", "explorer", "coder", "runner", "many model calls"):
        assert text in preview.body


def test_a_team_job_preview_without_a_reason_has_no_reason_line(tmp_path):
    preview = preview_for("start_team_job", {"task": "Survey the project", "why": ""}, tmp_path)
    assert "Why a team" not in preview.body and "Survey the project" in preview.body


def test_a_team_job_preview_never_raises_on_odd_arguments(tmp_path):
    assert preview_for("start_team_job", {"task": None, "why": 5}, tmp_path).title == "Start a team job"
    assert preview_for("start_team_job", "not a mapping", tmp_path).title
```

- [ ] **Step 6: Run them to see them fail**

Run: `python -m pytest tests/test_cli_preview.py -q -k team_job`
Expected: FAIL (the title is the tool name; the generic preview is used).

- [ ] **Step 7: Write the preview**

In `agentx_dev/cli/preview.py`, add before `_build`:

```python
TEAM_HELPERS_LINE = ("Helpers, one at a time: explorer (reads and searches), coder (edits files), "
                     "runner (runs commands and tests).")


def _team_job(args: Mapping[str, Any]) -> Preview:
    task = str(args.get("task") or "").strip()
    why = str(args.get("why") or "").strip()
    lines = [f"Task: {task}"]
    if why:
        lines.append(f"Why a team: {why}")
    lines += ["", TEAM_HELPERS_LINE,
              "A team job makes many model calls and can take several minutes. Every edit or command a "
              "helper tries is asked about as usual."]
    body, truncated = _whole("\n".join(lines))
    return Preview("Start a team job", body, "text", truncated, COMMAND_TOO_LONG if truncated else "")
```

and in `_build`, before the final `return _generic(tool, args)`:

```python
    if tool == "start_team_job":
        return _team_job(args)
```

(`_build` returns `_generic` for a non-mapping before reaching this line, which covers the "not a mapping" test; `str(None or "")` covers `None`; `5` becomes `"5"`.)

- [ ] **Step 8: Run both test files, then the full suite**

Run: `python -m pytest tests/test_cli_approval.py tests/test_cli_preview.py -q` then `python -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add agentx_dev/cli/approval.py agentx_dev/cli/preview.py tests/test_cli_approval.py tests/test_cli_preview.py
git commit -m "feat(cli): team tool kind in the gate and its approval preview"
```

---

### Task 2: The tool observer and `team.py` (roster, Supervisor run, result text, tool)

**Files:**
- Modify: `agentx_dev/cli/tools.py` (`gate_tool`, `build_tools`)
- Create: `agentx_dev/cli/team.py`
- Test: `tests/test_cli_tools.py` (append), `tests/test_cli_team.py` (new)

**Interfaces:**
- Consumes: `gate_tool(tool, gate, *, permissions=None, lock=None)`, `build_tools(cwd, add_dirs, gate)`, `Supervisor(model=, agents=, max_subtasks=, verbose=, max_subtask_retries=, max_plan_retries=, ask_user=, max_questions=)` with `.stream(task)` events `plan` / `dispatch` / `subtask_result` / `completion`; `SubtaskResult(agent, query, content, error, skipped, outcome)`; `SupervisorResult(content, subtasks, outcome)`; `_result_failed` from `agentx_dev.Supervisor`.
- Produces (used by Task 4):
  - `gate_tool(..., observer: Optional[Callable[[str, dict], None]] = None)` and `build_tools(cwd, add_dirs, gate, observer=None)`. The observer is called with `(tool_name, args_copy)` after the arguments are validated and before `gate.check`, also for calls the gate then refuses; an exception in it is ignored.
  - `team.py`: `TEAM_TOOL_NAME = "start_team_job"`, `MAX_TASK_CHARS = 4000`, `MAX_WHY_CHARS = 500`, `MAX_RESULT_CHARS = 6000`, `ROLES: tuple[Role, ...]` (explorer, coder, runner), `ROLE_MARK = "Team role:"`, `build_helpers(model, tools, addendum) -> Dict[str, Tuple[str, AgentRunner]]`, `format_result(result) -> str`, `run_team_job(task, *, model, tools, addendum, notify, ask_user=None, describe_error=str) -> str`, `TeamArgs`, `make_team_tool(run) -> StructuredTool` where `run(task: str, why: str) -> str`, `team_request(task) -> str`.
  - `notify(kind, data)` kinds: `"plan"` `{"steps": [{"agent", "query"}]}`, `"dispatch"` `{"agent", "query"}`, `"result"` `{"agent", "ok": bool, "text": str}`.

- [ ] **Step 1: Write the failing observer tests**

Append to `tests/test_cli_tools.py` (it already imports `Gate`, `build_tools`, `Path`):

```python
class TestObserver:
    def tools(self, tmp_path, observer, **gate_kw):
        gate = Gate(cwd=tmp_path, **gate_kw)
        return gate, {t.name: t.func for t in build_tools(tmp_path, [], gate, observer=observer)}

    def test_the_observer_sees_each_call_before_the_gate_decides(self, tmp_path):
        order = []
        gate, tools = self.tools(tmp_path, lambda name, args: order.append(("seen", name, dict(args))))
        real = gate.check

        def spy(tool, args):
            order.append(("gate", tool))
            return real(tool, args)

        gate.check = spy
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        assert "x" in tools["read_path"](path="a.txt")
        assert order[0][:2] == ("seen", "read_path") and order[0][2]["path"] == "a.txt"
        assert order[1] == ("gate", "read_path")

    def test_it_also_sees_calls_the_gate_refuses(self, tmp_path):
        seen = []
        gate, tools = self.tools(tmp_path, lambda name, args: seen.append(name), mode="plan")
        answer = tools["write_file"](path="b.txt", content="x", if_exists="refuse")
        assert seen == ["write_file"] and "plan mode" in answer
        assert not (tmp_path / "b.txt").exists()

    def test_an_observer_that_raises_changes_nothing(self, tmp_path):
        def boom(name, args):
            raise RuntimeError("display failed")

        _, tools = self.tools(tmp_path, boom)
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        assert "hello" in tools["read_path"](path="a.txt")

    def test_the_observer_gets_a_copy_it_cannot_use_to_change_the_call(self, tmp_path):
        def tamper(name, args):
            args["path"] = "other.txt"

        _, tools = self.tools(tmp_path, tamper)
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "other.txt").write_text("WRONG", encoding="utf-8")
        assert "hello" in tools["read_path"](path="a.txt")

    def test_invalid_arguments_are_not_shown_to_the_observer(self, tmp_path):
        seen = []
        _, tools = self.tools(tmp_path, lambda name, args: seen.append(name))
        assert tools["read_path"]().startswith("ERROR: invalid arguments")
        assert seen == []
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_tools.py -q -k TestObserver`
Expected: FAIL (`build_tools() got an unexpected keyword argument 'observer'`).

- [ ] **Step 3: Write the observer**

In `agentx_dev/cli/tools.py` change the `gate_tool` signature and body:

```python
def gate_tool(tool: StructuredTool, gate: Gate, *, permissions: Optional[Permissions] = None,
              lock: Optional[Lock] = None,
              observer: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> StructuredTool:
```

(add `Callable` to the `typing` import), extend the docstring with: "``observer(name, args)``, when given, is called with a copy of the validated arguments just before the gate check (also for a call the gate then refuses), so the screen can show a call as it happens; an error in it is ignored and it cannot change the call.", and insert between the `_as_the_tool_will_see_paths` block and the `try: with approvals:` block:

```python
        if observer is not None:
            try:
                observer(name, dict(args))
            except Exception:                       # showing a call must never change whether it runs
                pass
```

Change `build_tools`:

```python
def build_tools(cwd: Path, add_dirs: Sequence[Path], gate: Gate,
                observer: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> List[StructuredTool]:
```

and pass `observer=observer` to `gate_tool(...)` inside it.

- [ ] **Step 4: Run the observer tests and the existing tools tests**

Run: `python -m pytest tests/test_cli_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing `team.py` tests**

Create `tests/test_cli_team.py`:

```python
"""Teams mode: the roster, the Supervisor run, the result text and the tool."""

import pytest

from agentx_dev.cli import team as team_module
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.team import (MAX_RESULT_CHARS, MAX_TASK_CHARS, ROLE_MARK, ROLES, TEAM_TOOL_NAME, TeamArgs,
                                 build_helpers, format_result, make_team_tool, run_team_job, team_request)
from agentx_dev.cli.tools import build_tools, gate_tool
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import plan_json, router, step


@pytest.fixture
def tools(tmp_path):
    return build_tools(tmp_path, [], Gate(cwd=tmp_path))


def role_tools(name):
    return set(next(role for role in ROLES if role.name == name).tools)


def run(tmp_path, tools, model, notify=None, **kw):
    seen = []
    result = run_team_job("Survey the project", model=model, tools=tools, addendum="BASE PROMPT",
                          notify=notify or (lambda kind, data: seen.append((kind, data))), **kw)
    return result, seen


def test_the_roster_is_three_helpers_with_the_right_tools():
    assert [role.name for role in ROLES] == ["explorer", "coder", "runner"]
    explorer = role_tools("explorer")
    assert explorer == {"read_path", "list_directory", "find_files", "grep"}
    assert role_tools("coder") == explorer | {"write_file", "edit_file"}
    assert role_tools("runner") == explorer | {"run_shell", "run_python"}
    for role in ROLES:
        assert not set(role.tools) & {"delete_path", "move_file", TEAM_TOOL_NAME}


def test_each_helper_runner_gets_only_its_tools_and_its_role_text(tools):
    helpers = build_helpers(router(), tools, "BASE PROMPT")
    assert set(helpers) == {"explorer", "coder", "runner"}
    for name, (description, runner) in helpers.items():
        assert {t.name for t in runner.tools} == role_tools(name)
        assert description and "BASE PROMPT" in runner.system_addendum
        assert f"{ROLE_MARK} you are the {name} helper" in runner.system_addendum
        assert runner.max_iterations == 25


def test_a_job_plans_dispatches_and_returns_the_result(tmp_path, tools):
    model = router(plans=[plan_json(step("s1", "explorer", "look around"))], synth="All done.",
                   agent=lambda messages: make_final("found things"))
    result, seen = run(tmp_path, tools, model)
    assert result.startswith("Team job finished (outcome: done).")
    assert "1. explorer" in result and "All done." in result
    assert [kind for kind, _ in seen] == ["plan", "dispatch", "result"]
    assert seen[0][1]["steps"] == [{"agent": "explorer", "query": "look around"}]
    assert seen[1][1] == {"agent": "explorer", "query": "look around"}
    assert seen[2][1]["agent"] == "explorer" and seen[2][1]["ok"] is True and "found things" in seen[2][1]["text"]


def test_a_helper_uses_its_tools_and_the_observer_sees_them(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    seen = []
    tools = build_tools(tmp_path, [], Gate(cwd=tmp_path), observer=lambda name, args: seen.append(name))
    replies = [make_react_response("list_directory", {"path": "."}), make_final("listed")]
    model = router(plans=[plan_json(step("s1", "explorer", "list"))], synth="Done.",
                   agent=lambda messages: replies.pop(0))
    result, _ = run(tmp_path, tools, model)
    assert "listed" in result and seen == ["list_directory"]


def test_a_failed_step_is_reported_as_failed():
    from types import SimpleNamespace
    sub = SimpleNamespace(agent="coder", query="edit it", content="", error="boom", skipped=False, outcome="stuck",
                          superseded=False)
    result = SimpleNamespace(outcome="partial", subtasks=[sub], content="Partly done.")
    text = format_result(result)
    assert "outcome: partial" in text and "1. coder - failed" in text and "Partly done." in text


def test_a_long_result_is_cut_with_a_count():
    from types import SimpleNamespace
    result = SimpleNamespace(outcome="done", subtasks=[], content="x" * (MAX_RESULT_CHARS * 2))
    text = format_result(result)
    assert len(text) < MAX_RESULT_CHARS + 200 and "more characters" in text


class FakeSupervisor:
    """Stands in for the real Supervisor to test how a job ends."""
    behaviour = None
    closed = False

    def __init__(self, **kw):
        type(self).closed = False

    def stream(self, task):
        try:
            yield from type(self).behaviour()
        finally:
            type(self).closed = True


def test_a_supervisor_that_raises_gives_a_failure_result(tmp_path, tools, monkeypatch):
    def behaviour():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    FakeSupervisor.behaviour = staticmethod(behaviour)
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)
    result, _ = run(tmp_path, tools, router(), describe_error=lambda error: f"<{error}>")
    assert result == "Team job failed (RuntimeError): <boom>" and FakeSupervisor.closed


def test_a_supervisor_that_ends_without_a_result_says_so(tmp_path, tools, monkeypatch):
    FakeSupervisor.behaviour = staticmethod(lambda: iter([{"type": "plan_start"}]))
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)
    result, _ = run(tmp_path, tools, router())
    assert result == "Team job ended without a result."


def test_ctrl_c_propagates_and_the_stream_is_closed(tmp_path, tools, monkeypatch):
    def behaviour():
        yield {"type": "plan_start"}
        raise KeyboardInterrupt

    FakeSupervisor.behaviour = staticmethod(behaviour)
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, tools, router())
    assert FakeSupervisor.closed


def test_the_supervisor_is_built_with_the_agreed_limits(tmp_path, tools, monkeypatch):
    built = {}

    class Spy(FakeSupervisor):
        def __init__(self, **kw):
            built.update(kw)
            super().__init__(**kw)

    Spy.behaviour = staticmethod(lambda: iter([]))
    monkeypatch.setattr(team_module, "Supervisor", Spy)
    run(tmp_path, tools, router(), ask_user=lambda question: None)
    assert built["max_subtasks"] == 6 and built["max_subtask_retries"] == 0 and built["max_plan_retries"] == 1
    assert built["verbose"] is False and built["max_questions"] == 3 and callable(built["ask_user"])
    assert "spawn_config" not in built and "memory" not in built and "persistence" not in built
    assert set(built["agents"]) == {"explorer", "coder", "runner"}


def test_the_tool_has_a_schema_that_refuses_empty_and_over_long_tasks(tmp_path):
    tool = make_team_tool(lambda task, why: f"ran {task}")
    assert tool.name == TEAM_TOOL_NAME and "large" in tool.description.lower()
    assert tool.func(task="do it", why="") == "ran do it"
    with pytest.raises(Exception):
        TeamArgs(task="", why="")
    with pytest.raises(Exception):
        TeamArgs(task="x" * (MAX_TASK_CHARS + 1))
    gated = gate_tool(tool, Gate(mode="auto", cwd=tmp_path))
    assert gated.func(task="x" * (MAX_TASK_CHARS + 1)).startswith("ERROR: invalid arguments")


def test_team_request_names_the_tool_and_the_task():
    text = team_request("find the bug")
    assert TEAM_TOOL_NAME in text and "find the bug" in text
```

- [ ] **Step 6: Run them to see them fail**

Run: `python -m pytest tests/test_cli_team.py -q`
Expected: FAIL (`ModuleNotFoundError: agentx_dev.cli.team`).

- [ ] **Step 7: Write `team.py`**

Create `agentx_dev/cli/team.py`:

```python
"""Teams mode: a Supervisor with three helpers, started by the ``start_team_job`` tool.

The main chat agent calls ``start_team_job`` for a big job. This module builds the helpers and the Supervisor
for that one job, runs it to the end inside the tool call and returns one result string, so the conversation
already contains what the team did. It does no terminal I/O: progress goes out through a ``notify`` callback.
The roles steer the helpers; they are not a sandbox (``run_shell`` can write files). The gate, which every
helper tool goes through, is the boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from agentx_dev import AgentRunner, AgentType, Supervisor
from agentx_dev.Supervisor import _result_failed
from agentx_dev.Tools import StructuredTool

TEAM_TOOL_NAME = "start_team_job"
MAX_TASK_CHARS = 4000
MAX_WHY_CHARS = 500
MAX_RESULT_CHARS = 6000
HELPER_STEPS = 25
MAX_SUBTASKS = 6
MAX_QUESTIONS = 3
ROLE_MARK = "Team role:"
READ_TOOLS = ("read_path", "list_directory", "find_files", "grep")

Notify = Callable[[str, Dict[str, Any]], None]


@dataclass(frozen=True)
class Role:
    name: str
    description: str          # what the planner reads when it picks a helper
    tools: Tuple[str, ...]
    instructions: str         # what the helper itself reads


ROLES: Tuple[Role, ...] = (
    Role("explorer",
         "Reads and searches the project and cannot change anything. Use it to look things up, find code "
         "and summarise what exists.",
         READ_TOOLS,
         "You can only read: list, find, grep and read files. You cannot change anything, so report what you "
         "found, with file paths."),
    Role("coder",
         "Edits files (and can read and search). Use it to write or change code and documents. It cannot "
         "run commands.",
         READ_TOOLS + ("write_file", "edit_file"),
         "You change files with write_file and edit_file, after reading them. You cannot run commands, so say "
         "what you changed and what should be run to check it."),
    Role("runner",
         "Runs shell commands and Python (and can read and search). Use it to run tests, builds and scripts "
         "and report the output. It does not edit files.",
         READ_TOOLS + ("run_shell", "run_python"),
         "You run commands and code and report exactly what they printed, including failures. Do not edit "
         "project files; report what needs changing instead."),
)


def role_prompt(role: Role) -> str:
    return (f"\n\n{ROLE_MARK} you are the {role.name} helper on a team led by a supervisor. {role.instructions} "
            "Finish with a short report; the supervisor passes it on.")


def build_helpers(model: Any, tools: Sequence[Any], addendum: str) -> Dict[str, Tuple[str, AgentRunner]]:
    """One ``AgentRunner`` per role, each holding only its slice of ``tools`` (the already gated tools, picked by
    name), so a helper's calls go through the same gate as the main chat. ``addendum`` is the chat's own system
    addendum (the project instructions); the role text follows it."""
    by_name = {tool.name: tool for tool in tools}
    helpers: Dict[str, Tuple[str, AgentRunner]] = {}
    for role in ROLES:
        chosen = [by_name[name] for name in role.tools if name in by_name]
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=chosen,
                             system_addendum=(addendum or "") + role_prompt(role),
                             max_iterations=HELPER_STEPS, auto_cache=False, verbose=False,
                             parallel_tool_workers=1)
        helpers[role.name] = (role.description, runner)
    return helpers


def _first_line(text: Any, limit: int = 160) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def format_result(result: Any) -> str:
    """The one string the main agent gets back: the outcome, each step's fate, and the final answer."""
    lines = [f"Team job finished (outcome: {getattr(result, 'outcome', 'done')})."]
    subtasks = list(getattr(result, "subtasks", None) or [])
    if subtasks:
        lines.append("Steps:")
        for number, sub in enumerate(subtasks, 1):
            if getattr(sub, "skipped", False):
                state = "skipped"
            elif _result_failed(sub):
                state = "failed: " + _first_line(getattr(sub, "error", "") or getattr(sub, "outcome", ""), 80)
            else:
                state = "done"
            lines.append(f"{number}. {sub.agent} - {state}: {_first_line(sub.query)}")
    lines += ["", "Result:", str(getattr(result, "content", ""))]
    text = "\n".join(lines)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... (cut; {len(text) - MAX_RESULT_CHARS} more characters)"
    return text


def run_team_job(task: str, *, model: Any, tools: Sequence[Any], addendum: str, notify: Notify,
                 ask_user: Optional[Callable[[str], Optional[str]]] = None,
                 describe_error: Callable[[BaseException], str] = str) -> str:
    """Plan ``task``, run the helpers one at a time and return the result text. Progress goes to ``notify``.

    Ctrl+C (``KeyboardInterrupt``) and ``SystemExit`` propagate, with the Supervisor's stream closed; any other
    error becomes a short failure result for the main agent to read."""
    helpers = build_helpers(model, tools, addendum)
    supervisor = Supervisor(model=model, agents=helpers, max_subtasks=MAX_SUBTASKS, verbose=False,
                            max_subtask_retries=0, max_plan_retries=1, ask_user=ask_user,
                            max_questions=MAX_QUESTIONS)
    events = supervisor.stream(task)
    result = None
    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "plan":
                steps = [{"agent": str(step.get("agent", "")), "query": str(step.get("query", ""))}
                         for step in (event.get("plan") or []) if isinstance(step, dict)]
                notify("plan", {"steps": steps})
            elif kind == "dispatch":
                notify("dispatch", {"agent": str(event.get("agent", "")), "query": str(event.get("query", ""))})
            elif kind == "subtask_result":
                sub = event.get("result")
                if sub is not None and getattr(sub, "agent", "") != "__spawn__":
                    failed = bool(getattr(sub, "skipped", False)) or _result_failed(sub)
                    text = getattr(sub, "error", "") or getattr(sub, "content", "")
                    notify("result", {"agent": str(sub.agent), "ok": not failed, "text": _first_line(text)})
            elif kind == "completion":
                result = event.get("result")
    except Exception as error:                     # KeyboardInterrupt and SystemExit are not Exceptions
        return f"Team job failed ({type(error).__name__}): {describe_error(error)}"
    finally:
        events.close()
    if result is None:
        return "Team job ended without a result."
    return format_result(result)


class TeamArgs(BaseModel):
    task: str = Field(..., min_length=1, max_length=MAX_TASK_CHARS,
                      description="The whole job, with enough detail for a team that has not seen this "
                                  "conversation.")
    why: str = Field("", max_length=MAX_WHY_CHARS,
                     description="One sentence: why this job is too big to do alone.")


TOOL_DESCRIPTION = (
    "Hand a LARGE job to a team of helpers (an explorer that reads, a coder that edits files, a runner that "
    "runs commands and tests), led by a supervisor who plans the steps. Use it only for large, multi-part "
    "work: several files or areas, research plus changes plus tests. Do small and medium jobs yourself. The "
    "user is asked before it starts, it makes many model calls, and only one team job can run per user message. "
    "You get the supervisor's result back; then summarise it for the user or continue.")


def make_team_tool(run: Callable[[str, str], str]) -> StructuredTool:
    """The ``start_team_job`` tool; ``run(task, why)`` does the work. Wrap it with ``gate_tool`` before use."""
    def start_team_job(task: str, why: str = "") -> str:
        return run(task, why)

    return StructuredTool(func=start_team_job, args_schema=TeamArgs, name=TEAM_TOOL_NAME,
                          description=TOOL_DESCRIPTION)


def team_request(task: str) -> str:
    """The message ``/team <task>`` sends the main agent."""
    return (f"Call the {TEAM_TOOL_NAME} tool now to hand this job to the team, then tell me what the team "
            f"did: {task}")
```

- [ ] **Step 8: Run the team tests**

Run: `python -m pytest tests/test_cli_team.py -q`
Expected: PASS. If the router-driven tests fail because the real `Supervisor` rejects something (for example a runner attribute it needs), read the error and adapt `build_helpers`, not the Supervisor.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add agentx_dev/cli/tools.py agentx_dev/cli/team.py tests/test_cli_tools.py tests/test_cli_team.py
git commit -m "feat(cli): team helpers, supervisor run and the start_team_job tool"
```

---

### Task 3: Showing a team job on the screen

**Files:**
- Modify: `agentx_dev/cli/ui.py` (`Screen`, `_request_body`, `make_asker`)
- Test: `tests/test_cli_ui.py` (append)

**Interfaces:**
- Consumes: `Screen(console)`, `plain`, `_short`, `_print`, `bullet`, `subject_of`, `Request`, `Preview`, `make_asker(console, read_line, cwd, select=None)`.
- Produces (used by Task 4):
  - `Screen.team_plan(steps: list[dict])`, `Screen.team_dispatch(agent: str, query: str)`, `Screen.team_result(agent: str, ok: bool, text: str)`, `Screen.helper_call(helper: str, name: str, args: dict)`, `Screen.team_question(question: str)`.
  - `make_asker(console, read_line, cwd, select=None, helper=None)` where `helper` is a callable returning the current helper's name ("" outside a team job); the approval panel then has an `Helper: <name>` line after `Action:`. A `team`-kind request prints no `Subject:` line (the task is already in the body).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_ui.py` (`io`, `Console`, `Path`, `Request`, `Screen`, `make_asker` are already imported there):

```python
class TestTeamScreen:
    def screen(self):
        out = io.StringIO()
        return Screen(Console(file=out, width=100, force_terminal=False, color_system=None)), out

    def test_the_plan_the_dispatch_and_the_result_are_drawn(self):
        screen, out = self.screen()
        screen.team_plan([{"agent": "explorer", "query": "look around"}, {"agent": "coder", "query": "fix it"}])
        screen.team_dispatch("explorer", "look around")
        screen.team_result("explorer", True, "found 3 files")
        screen.team_result("coder", False, "boom")
        text = out.getvalue()
        assert "Team plan" in text and "1. explorer: look around" in text and "2. coder: fix it" in text
        assert "-> explorer: look around" in text
        assert "done (explorer): found 3 files" in text and "failed (coder): boom" in text

    def test_a_helper_tool_call_shows_the_helper_the_tool_and_what_it_acts_on(self):
        screen, out = self.screen()
        screen.helper_call("coder", "write_file", {"path": "src/app.py", "content": "x"})
        line = out.getvalue()
        assert "[coder]" in line and "write_file" in line and "src/app.py" in line

    def test_a_question_is_drawn_with_its_text(self):
        screen, out = self.screen()
        screen.team_question("Which database?")
        assert "Which database?" in out.getvalue()

    def test_markup_and_escape_codes_in_team_text_stay_inert(self):
        screen, out = self.screen()
        hostile = "[red]boom[/red] \x1b[2J \x07"
        screen.team_plan([{"agent": hostile, "query": hostile}])
        screen.team_dispatch(hostile, hostile)
        screen.team_result(hostile, False, hostile)
        screen.helper_call(hostile, hostile, {"path": hostile})
        screen.team_question(hostile)
        text = out.getvalue()
        assert "[red]boom[/red]" in text            # printed as text, not read as markup
        assert "\x1b" not in text and "\x07" not in text

    def test_odd_arguments_never_raise(self):
        screen, _ = self.screen()
        screen.team_plan(None)
        screen.team_plan([None, 5, {"agent": None}])
        screen.helper_call(None, None, None)
        screen.team_result(None, True, None)


class TestHelperLineInThePrompt:
    def ask(self, helper, kind="edit", tool="write_file", args=None):
        out = io.StringIO()
        ask = make_asker(Console(file=out, width=100, force_terminal=False, color_system=None),
                         lambda prompt="": "y", Path("."), helper=helper)
        request = Request(tool=tool, kind=kind, subject="notes.txt",
                          args=args or {"path": "notes.txt", "content": "hi", "if_exists": "refuse"},
                          remember=False, grant="")
        ask(request)
        return out.getvalue()

    def test_the_panel_names_the_helper_that_is_asking(self):
        text = self.ask(lambda: "coder")
        assert "Helper: coder" in text and text.index("Action:") < text.index("Helper:")

    def test_no_helper_line_outside_a_team_job(self):
        assert "Helper:" not in self.ask(lambda: "")
        assert "Helper:" not in self.ask(None)

    def test_a_helper_name_with_escape_codes_is_defused(self):
        text = self.ask(lambda: "co\x1b[2Jder")
        assert "\x1b" not in text and "Helper: co" in text

    def test_a_team_request_prints_no_subject_line(self):
        text = self.ask(None, kind="team", tool="start_team_job",
                        args={"task": "Survey the project", "why": "big"})
        assert "Survey the project" in text and "Subject:" not in text
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cli_ui.py -q -k "TestTeamScreen or TestHelperLine"`
Expected: FAIL (`Screen` has no `team_plan`; `make_asker` has no `helper`).

- [ ] **Step 3: Write the Screen methods**

In `agentx_dev/cli/ui.py`, add to `class Screen` (after `error`):

```python
    def team_plan(self, steps: Any) -> None:
        _print(self.console, Text("Team plan:", style="bold magenta"))
        for number, step in enumerate(steps if isinstance(steps, (list, tuple)) else [], 1):
            if not isinstance(step, dict):
                continue
            _print(self.console, Text(f"  {number}. {_short(step.get('agent'), 30)}: "
                                      f"{_short(step.get('query'), 200)}", style="dim"))

    def team_dispatch(self, agent: Any, query: Any) -> None:
        _print(self.console, Text(f"-> {_short(agent, 30)}: {_short(query, 200)}", style="bold magenta"))

    def team_result(self, agent: Any, ok: bool, text: Any) -> None:
        _print(self.console, Text(f"   {'done' if ok else 'failed'} ({_short(agent, 30)}): {_short(text, 160)}",
                                  style="dim" if ok else "red"))

    def helper_call(self, helper: Any, name: Any, args: Any) -> None:
        """A helper's tool call, drawn like the main chat's with the helper's name in front."""
        try:
            summary = _short(subject_of(str(name), args if isinstance(args, dict) else {}))
        except Exception:
            summary = ""
        line = Text(f"{bullet(self.console)} [{_short(helper, 30)}] {_short(name, 40)}", style="bold")
        if summary:
            line.append(f"  {summary}", style="dim")
        _print(self.console, line)

    def team_question(self, question: Any) -> None:
        _print(self.console, Text("Team question: " + plain(_nothing_if_none(question), keep_newlines=True),
                                  style="bold yellow"))
```

Note: a `Text` is printed as text, so the `[coder]` in `helper_call` is never read as markup.

- [ ] **Step 4: Write the helper line and the asker parameter**

Change `_request_body`:

```python
def _request_body(request: Request, preview: Preview, helper: str = "") -> Text:
    """The panel's content. ... (keep the existing docstring) ..."""
    body = Text()
    action = plain(preview.title)
    subject = plain(request.subject)
    body.append("Action: ", style="bold")
    body.append(action + "\n")
    if helper:
        body.append("Helper: ", style="bold")
        body.append(plain(helper) + "\n")
    # The subject is what the gate judged. Skip it only when it adds nothing: it is the action text itself,
    # the very command the body already shows, or a team job (its task is the body's first line).
    shown_command = preview.kind == "command" and subject == " ".join(plain(preview.body).split())
    if subject != action and not shown_command and request.kind != "team":
        body.append("Subject: ", style="bold")
        body.append(subject + "\n")
    if preview.body:
        body.append("\n")
        body.append_text(_preview_text(preview))
    return body
```

(Keep the existing comment lines and the existing `Subject:` code exactly as they are, only add the `helper` argument, the `if helper:` block and the `request.kind != "team"` condition.)

Change `make_asker`'s signature and its panel call:

```python
def make_asker(console: Console, read_line: Optional[Callable[[str], str]],
               cwd: Path,
               select: Optional[Callable[..., Optional[int]]] = None,
               helper: Optional[Callable[[], str]] = None) -> Callable[[Request], Answer]:
```

and inside `ask`, replace `_request_body(request, preview)` with:

```python
        try:
            who = str(helper() or "") if helper is not None else ""
        except Exception:
            who = ""
        _print(console, Panel(_request_body(request, preview, who), title=Text(plain(request.tool)),
                              title_align="left"))
```

(That replaces the existing `_print(console, Panel(_request_body(request, preview), ...))` call; keep `title_align="left"`.) Add one sentence to `make_asker`'s docstring: "``helper`` (optional) returns the name of the team helper whose step is running, shown as a ``Helper:`` line."

- [ ] **Step 5: Run the UI tests, then the whole suite**

Run: `python -m pytest tests/test_cli_ui.py -q` then `python -m pytest -q`
Expected: PASS. (`tests/test_cli_app.py` has a `spy_asker(console, read_line, cwd, select=None)`; it still works because Task 4, not this task, starts passing `helper=`.)

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/cli/ui.py tests/test_cli_ui.py
git commit -m "feat(cli): draw team plans, steps and helper calls; name the helper in approval prompts"
```

---

### Task 4: Wiring the app, `/team`, and the end-to-end tests

**Files:**
- Modify: `agentx_dev/cli/app.py`, `agentx_dev/cli/commands.py`, `agentx_dev/cli/context.py`
- Modify (test fakes): `tests/test_cli_app.py` (the `spy_asker` and `spy_tools` signatures), `tests/test_cli_commands.py` (the `Ctx` fake, the command list)
- Test: `tests/test_cli_team_app.py` (new)

**Interfaces:**
- Consumes: everything from Tasks 1 - 3; `ChatApp`, `make_app(tmp_path, lines, model, **kw)`, `Lines(...)` from `tests/test_cli_app.py`; `tests.subagent_helpers` (`PLANNER_MARK`, `SYNTH_MARK`, `plan_json`, `step`).
- Produces: `ChatApp.base_tools`, `ChatApp.tools` (ten gated tools + the gated team tool), `ChatApp._team_helper` (`""` outside a job), `ChatApp._team_used`, `ChatApp.run_team_command(task)`; the `/team` command (`command_names()` gains `"/team"` after `"/init"`).

- [ ] **Step 1: Update the two test fakes that the new wiring would break**

In `tests/test_cli_app.py` change `spy_asker` and `spy_tools` (they sit in `test_every_part_is_built_for_the_same_folder`):

```python
    def spy_asker(console, read_line, cwd, select=None, helper=None):
        seen["asker"] = cwd
        return real_asker(console, read_line, cwd, select=select, helper=helper)

    def spy_tools(cwd, add_dirs, gate, observer=None):
        seen["tools"], seen["tools_gate"] = cwd, gate
        return real_tools(cwd, add_dirs, gate, observer=observer)
```

In `tests/test_cli_commands.py` add to the `Ctx` fake:

```python
    def run_team_command(self, task): self.calls.append(("team", task))
```

and change the expected list in `test_help_lists_every_command` to
`["/help", "/clear", "/model", "/mode", "/settings", "/resume", "/sessions", "/init", "/team", "/exit"]`.

- [ ] **Step 2: Write the failing command tests**

Append to `tests/test_cli_commands.py`:

```python
def test_team_sends_the_task_to_the_app():
    ctx = Ctx()
    assert run_command(ctx, "/team survey the project and fix the tests") is True
    assert ctx.calls == [("team", "survey the project and fix the tests")]


def test_team_without_a_task_explains_how_to_use_it():
    ctx = Ctx()
    assert run_command(ctx, "/team") is True
    assert ctx.calls == [] and "/team <task>" in ctx.said[-1]
```

- [ ] **Step 3: Write the failing end-to-end tests**

Create `tests/test_cli_team_app.py`:

```python
"""A team job through the whole app: the main agent calls start_team_job, the team runs, the result is in the
conversation. Scripted models only."""

import json

from agentx_dev.cli.sessions import list_sessions
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import PLANNER_MARK, SYNTH_MARK, plan_json, step
from tests.test_cli_app import Lines, make_app

CALL = {"task": "Survey the project and report", "why": "it spans many areas"}


def team_model(steps, synth="The survey is complete.", helper=None, main=("call", "final")):
    """One model that plays the main agent, the planner, the synthesizer and the helpers.

    ``main`` is what the main agent does on its successive model calls: "call" starts a team job, "final"
    answers. After the list is used up it answers."""
    state = {"main": 0, "helper_calls": 0}

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first:
            return plan_json(*steps)
        if SYNTH_MARK in first:
            return synth
        if "Team role:" in first:
            state["helper_calls"] += 1
            return (helper or (lambda messages, n: make_final("helper report")))(messages, state["helper_calls"])
        state["main"] += 1
        action = main[state["main"] - 1] if state["main"] <= len(main) else "final"
        if action == "call":
            return make_react_response("start_team_job", CALL)
        return make_final("The team finished; here is the summary.")

    model = MockModel(script=script)
    model.state = state
    return model


def history_text(app):
    return json.dumps(app.session.history, default=str)


def test_the_main_agent_hands_a_big_job_to_the_team_and_the_result_is_in_the_conversation(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, home = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=Lines("y"))
    assert app.start() == 0
    text = out.getvalue()
    assert "Team plan" in text and "-> explorer: look around" in text and "done (explorer)" in text
    assert "The team finished; here is the summary." in text
    assert "Team job finished (outcome: done)" in history_text(app)          # the tool result is in the chat
    saved = list_sessions(home, app.cwd)
    assert saved and "Team job finished" in saved[0].path.read_text(encoding="utf-8")


def test_the_user_can_decline_the_hand_off(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=Lines("n", ""))
    assert app.start() == 0
    assert "Team plan" not in out.getvalue()
    assert "declined" in history_text(app).lower()
    assert model.state["helper_calls"] == 0


def test_plan_mode_refuses_the_hand_off_without_asking(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="plan",
                              read_short=lambda prompt="": asked.append(prompt) or "y")
    assert app.start() == 0
    assert "Team plan" not in out.getvalue() and asked == []
    assert "plan mode" in history_text(app)


def test_a_deny_rule_turns_the_feature_off(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, deny=["start_team_job:*"],
                              read_short=Lines("y"))
    assert app.start() == 0
    assert "Team plan" not in out.getvalue() and "deny rule" in history_text(app)


def test_auto_mode_starts_the_team_without_asking(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              read_short=lambda prompt="": asked.append(prompt) or "n")
    assert app.start() == 0
    assert "Team plan" in out.getvalue() and asked == []


def test_a_second_team_job_in_the_same_turn_is_refused(tmp_path):
    model = team_model([step("s1", "explorer", "look around")], main=("call", "call", "final"))
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == 1
    assert "already ran in this turn" in history_text(app)


def test_the_next_message_may_start_another_job(tmp_path):
    model = team_model([step("s1", "explorer", "look around")], main=("call", "final", "call", "final"))
    app, out, _, _ = make_app(tmp_path, Lines("first big job", "second big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == 2
    assert "already ran in this turn" not in history_text(app)


def test_a_helper_edit_still_asks_in_default_mode_and_the_prompt_names_the_helper(tmp_path):
    def coder(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": "team.txt", "content": "hi", "if_exists": "refuse"})
        return make_final("wrote team.txt")

    model = team_model([step("s1", "coder", "write team.txt")], helper=coder)
    app, out, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model,
                                read_short=Lines("y", "y"))                 # the hand-off, then the write
    assert app.start() == 0
    assert (cwd / "team.txt").read_text(encoding="utf-8") == "hi"
    text = out.getvalue()
    assert "Helper: coder" in text and "[coder] write_file" in text
    assert text.index("[coder] write_file") < text.index("Helper: coder")      # the call shows before its prompt


def test_a_helper_cannot_write_where_the_default_denies_even_in_auto_mode(tmp_path):
    told = []

    def coder(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": ".git/config", "content": "x",
                                                       "if_exists": "overwrite"})
        told.append(str(messages[-1]["content"]))               # what the helper was told about its call
        return make_final("tried")

    (tmp_path / "proj" / ".git").mkdir(parents=True)
    model = team_model([step("s1", "coder", "edit git config")], helper=coder)
    app, out, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert not (cwd / ".git" / "config").exists()
    assert told and "deny rule" in told[0]


def test_the_explorer_has_no_way_to_write(tmp_path):
    def explorer(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": "x.txt", "content": "hi", "if_exists": "refuse"})
        return make_final("could not write")

    model = team_model([step("s1", "explorer", "try to write")], helper=explorer)
    app, _, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert not (cwd / "x.txt").exists()


def test_the_planner_can_ask_the_user_and_an_empty_answer_skips_it(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    original = model._script_fn

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first and not asked:
            asked.append(True)
            return json.dumps({"plan": [], "ask": [{"question": "Which folder?", "why": "to scope the survey"}]})
        return original(messages)

    model._script_fn = script
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              read_short=Lines(""))
    assert app.start() == 0
    assert "Which folder?" in out.getvalue()


def test_ctrl_c_in_the_middle_of_a_job_aborts_the_turn_and_the_next_message_works(tmp_path):
    def helper(messages, n):
        raise KeyboardInterrupt

    model = team_model([step("s1", "explorer", "look around")], helper=helper)
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "next message", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert "Turn interrupted" in out.getvalue() and "here is the summary" in out.getvalue()
    assert app._team_helper == ""


def test_team_command_asks_the_main_agent_to_call_the_tool(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("/team survey the project", "/exit"), model, mode="auto")
    assert app.start() == 0
    sent = " ".join(str(message.get("content")) for call in model.calls for message in call
                    if message.get("role") == "user")
    assert "start_team_job" in sent and "survey the project" in sent
    assert "Team plan" in out.getvalue()


def test_the_team_tool_is_in_the_main_tool_list_but_not_in_any_helper(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), model)
    names = [tool.name for tool in app.tools]
    assert names.count("start_team_job") == 1 and len(names) == len(app.base_tools) + 1
    assert "start_team_job" not in [tool.name for tool in app.base_tools]
```

- [ ] **Step 4: Run them to see them fail**

Run: `python -m pytest tests/test_cli_commands.py tests/test_cli_team_app.py -q`
Expected: FAIL (`ChatApp` has no `base_tools`, there is no `/team`, the model never gets the tool).

- [ ] **Step 5: Add `/team` and the prompt bullet**

In `agentx_dev/cli/commands.py` add before the `COMMANDS` tuple:

```python
def _team(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice("Usage: /team <task>. Hands a big job to a team of helpers (explorer, coder, runner). "
                   "You are asked before it starts. Small jobs are better done in the normal chat.")
        return
    ctx.run_team_command(arg)
```

and in `COMMANDS`, after the `init` command and before `exit`:

```python
    Command("team", "Hand a big job to a team of helpers: /team <task>", _team),
```

In `agentx_dev/cli/context.py` add one bullet to `BASE_PROMPT` (after the "Be brief" bullet):

```
- For a large, multi-part job (several files or areas: research, changes and tests together), call start_team_job to hand it to a team of helpers instead of doing it all yourself. Do small and medium jobs yourself.
```

- [ ] **Step 6: Wire the app**

In `agentx_dev/cli/app.py`:

Imports: change `from agentx_dev.cli.tools import build_tools` to `from agentx_dev.cli.tools import build_tools, gate_tool`, and add
`from agentx_dev.cli.team import make_team_tool, run_team_job, team_request`.

Add a constant near `INIT_PROMPT`:

```python
TEAM_ALREADY_RAN = ("Refused: a team job already ran in this turn. Use its result, or wait for the user's "
                    "next message before starting another.")
```

In `ChatApp.__init__`, replace the `make_asker(...)` line and the `self.tools = ...` line:

```python
        self._team_helper = ""             # the helper whose step is running; "" outside a team job
        self._team_used = False            # a turn may start one team job
        asker = make_asker(console, self.read_short, self.gate.cwd, select=select,
                           helper=lambda: self._team_helper)
```

(that replaces `asker = make_asker(console, self.read_short, self.gate.cwd, select=select)`), and

```python
        self.base_tools = build_tools(self.cwd, self.add_dirs, self.gate, observer=self._observe_tool)
        self.tools = [*self.base_tools, gate_tool(make_team_tool(self._start_team_job), self.gate)]
```

(that replaces `self.tools = build_tools(self.cwd, self.add_dirs, self.gate)`).

Add these methods to `ChatApp` (after `run_init`):

```python
    def run_team_command(self, task: str) -> None:
        """``/team <task>``: ask the main agent to call the team tool for ``task`` (same prompt, gate and history)."""
        self.send(team_request(task))

    # -- team jobs ----------------------------------------------------------

    def _observe_tool(self, name: str, args: dict) -> None:
        """Called by the tool wrappers before the gate check. During a team job it shows the helper's call."""
        if not self._team_helper:
            return
        self.progress.stop()
        self.screen.helper_call(self._team_helper, name, args)
        self.progress.start(f"Running {self._team_helper}")

    def _team_notify(self, kind: str, data: dict) -> None:
        self.progress.stop()
        if kind == "plan":
            self.screen.team_plan(data.get("steps"))
            self.progress.start("Starting the team")
        elif kind == "dispatch":
            self._team_helper = str(data.get("agent", ""))
            self.screen.team_dispatch(data.get("agent"), data.get("query"))
            self.progress.start(f"Running {self._team_helper}")
        elif kind == "result":
            self._team_helper = ""
            self.screen.team_result(data.get("agent"), bool(data.get("ok")), data.get("text"))
            self.progress.start("Team is working")

    def _team_question(self, question: str) -> Optional[str]:
        """The planner or a helper needs a fact: ask in the terminal. Enter, Ctrl+D and Ctrl+C skip it."""
        with self.progress.paused():
            self.screen.team_question(question)
            try:
                answer = self.read_short("Answer (Enter to skip): ")
            except (EOFError, KeyboardInterrupt):
                return None
        return str(answer).strip() or None

    def _start_team_job(self, task: str, why: str) -> str:
        """The ``start_team_job`` tool: run the team and hand the result text back to the main agent."""
        if self._team_used:
            return TEAM_ALREADY_RAN
        if self.model is None:
            return "Refused: no model is available for a team job."
        self._team_used = True
        self.progress.start("Planning the team job")
        try:
            return run_team_job(task, model=self.model, tools=self.base_tools, addendum=self.addendum,
                                notify=self._team_notify, ask_user=self._team_question,
                                describe_error=self._safe)
        finally:
            self._team_helper = ""
            self.progress.start("Thinking")
```

In `send(self, text)` set the per-turn flag at the start:

```python
        assert self.session is not None
        self._team_used = False                        # a new message may start a new team job
```

- [ ] **Step 7: Run the new tests and fix what they show**

Run: `python -m pytest tests/test_cli_commands.py tests/test_cli_team_app.py tests/test_cli_app.py -q`
Expected: PASS. Likely adjustments, in order of probability:
- The planner-question test: a planner that wants to ask answers `{"plan": [], "ask": [{"question", "why"}]}` and is then asked to plan again (`Supervisor._plan_once`); the test's first planner reply is that, the second is the normal plan. If the question never shows, check that `ask_user` reached the Supervisor (the app passes `ask_user=self._team_question`).
- If the real `AgentRunner` converts a `KeyboardInterrupt` raised by a model into something else, raise it from a patched `app_module.run_team_job` instead, and say so in the report.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add agentx_dev/cli/app.py agentx_dev/cli/commands.py agentx_dev/cli/context.py tests/test_cli_app.py tests/test_cli_commands.py tests/test_cli_team_app.py
git commit -m "feat(cli): team jobs from the chat - start_team_job tool and /team"
```

---

### Task 5: Docs, changelog and the by-hand checklist

**Files:**
- Modify: `docs/guides/cli.md`, `CHANGELOG.md`, `README.md`, `docs/superpowers/specs/2026-10-11-cli-teams-mode-design.md` (small corrections only), `host/data.js` (regenerated)

**Interfaces:**
- Consumes: the behaviour of Tasks 1 - 4 (read the code, not only this plan, before documenting).
- Produces: user-facing documentation; no code.

- [ ] **Step 1: Write the guide section**

In `docs/guides/cli.md`, add a "Team jobs" section after the slash commands section (match the style of the neighbouring sections; read the first 40 lines of the file first). It must state, accurately and from the code:

1. What it is: for a big, multi-part job the normal agent can call `start_team_job`; a supervisor plans up to 6 steps and three helpers run them one at a time. `/team <task>` asks for it by hand.
2. The three helpers and their tools (explorer: read, list, find, grep; coder: those plus `write_file`, `edit_file`; runner: those plus `run_shell`, `run_python`); none can delete, move or start another team. The roles steer, the gate protects (`run_shell` can write files).
3. The approval: shown with the task, the reason, the helpers and a cost warning ("many model calls, several minutes"); asks in `default` and `accept-edits`, refused in `plan`, runs in `auto`; no "don't ask again". Every edit and command a helper tries is asked about exactly as in the chat, the prompt gets a `Helper: coder` line, protected files and deny rules apply.
4. What you see: the plan, `-> coder: ...` lines, each helper tool call as `● [coder] write_file path`, a `done (explorer): ...` line per step, a spinner "Running coder".
5. Questions: the planner or a helper may ask you for a fact; Enter skips.
6. The result is in the conversation (so "now fix the second item" works) and in the saved session.
7. One team job per message; Ctrl+C stops it and aborts the turn (changes already made stay on disk; the turn is not recorded).
8. Turning it off: a deny rule, for example `"deny": ["start_team_job:*"]` in `~/.agentx/settings.json`.
9. Limits: helpers run one at a time; no invented helpers; no memory between jobs; each step is a fresh helper (the supervisor passes earlier results along); a failed step is not retried.

Add "Team job" to the "Limits in this version" list wording where the guide says "No teams mode" (replace that bullet with: "Teams mode is limited: helpers run one at a time and the roster is fixed (see Team jobs).").

Add to the "Try it by hand" checklist (continue the numbering):
- Ask for something big ("survey this project, then add a docstring to each module and run the tests"): the hand-off prompt appears with the task and helpers; approve; the plan, the `-> helper` lines and the `[helper]` tool bullets show; a write asks with `Helper: coder`.
- `/team` with no task prints the usage; `/team <task>` triggers the same prompt.
- Add `"deny": ["start_team_job:*"]` to your settings and start again: the hand-off is refused.
- Press Ctrl+C during a job: "Turn interrupted", and the next message works.

- [ ] **Step 2: Update the other docs**

- `CHANGELOG.md`: under `## [3.7.0] - Unreleased` / `### Added`, in the list of `agentx` items add: "**Team jobs.** For a big job the chat agent can call `start_team_job` (the user approves each hand-off; `/team <task>` does it by hand): a `Supervisor` plans up to six steps and three helpers (explorer reads, coder edits, runner runs commands) carry them out one at a time. Every helper call goes through the same gate, modes, deny rules and prompts as the chat; the result joins the conversation. A deny rule `start_team_job:*` turns it off." Also add `/team` to the slash-command list line there.
- `README.md`: in "The agentx command" section add one sentence: "For big jobs it can hand the work to a team of helpers (with your approval); see the guide."
- The spec: in section 2 non-goals nothing changes; in section 7 add "(`/team` is the same path; the planner's questions use `Answer (Enter to skip):`)" if not already stated; fix any statement that no longer matches the code after implementation (for example the exact wording of messages).

- [ ] **Step 3: Regenerate, test, commit**

Run: `python host/build_data.py` (regenerates `host/data.js`), then `python -m pytest -q`
Expected: PASS.

```bash
git add docs/guides/cli.md CHANGELOG.md README.md docs/superpowers/specs/2026-10-11-cli-teams-mode-design.md host/data.js
git commit -m "docs: team jobs in the agentx guide, README and changelog"
```

---

## Self-review (done while writing this plan)

- **Spec coverage:** sections 2-3 (goals, approach) are Tasks 2 and 4; section 4 (structure) is the File Structure table; section 5 (tool and gate: modes, deny, no always, 4,000/500 caps, one per turn) is Tasks 1, 2 (schema caps) and 4 (flag); section 6 (helpers, Supervisor settings) is Task 2; section 7 (running a job: plan, dispatch, observer, helper line, results, questions, final text, `/team`) is Tasks 2 - 4; section 8 (errors, interruption) is Tasks 2 and 4; section 9 (testing list) maps to the tests in Tasks 1 - 4; section 10 (docs) is Task 5. The spec's "no model: short refusal" is `_start_team_job` in Task 4 (tested indirectly; add a direct test if `model is None` is reachable in `make_app`).
- **Placeholder scan:** none; every code step has code.
- **Type consistency:** `notify(kind, data)` kinds and keys (`plan.steps`, `dispatch.agent/query`, `result.agent/ok/text`) match between `team.py` (Task 2), `Screen` (Task 3) and `_team_notify` (Task 4); `make_asker(..., helper=)` (Task 3) matches the app call and the updated `spy_asker` (Task 4); `build_tools(..., observer=)` (Task 2) matches the app call and the updated `spy_tools`; `team_request`, `make_team_tool`, `run_team_job` signatures match their uses.
