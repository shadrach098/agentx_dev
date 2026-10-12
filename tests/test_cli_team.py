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
    result, events = run(tmp_path, tools, model)
    assert "Done." in result and seen == ["list_directory"]
    assert events[-1][0] == "result" and "listed" in events[-1][1]["text"]


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


def test_the_tool_args_ignore_extra_keys_and_keep_the_why_limit():
    args = TeamArgs(task="do it", why="because", surprise="ignored")
    assert args.task == "do it" and not hasattr(args, "surprise")
    with pytest.raises(Exception):
        TeamArgs(task="do it", why="x" * (team_module.MAX_WHY_CHARS + 1))
    TeamArgs(task="x" * MAX_TASK_CHARS)
