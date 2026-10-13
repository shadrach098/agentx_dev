"""Teams mode: the roster, the Supervisor run, the result text and the tool."""

import pytest

from agentx_dev.cli import team as team_module
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.team import (HELPER_STEPS, MAX_RESULT_CHARS, MAX_TASK_CHARS, ROLE_MARK, ROLES, TEAM_TOOL_NAME,
                                 TeamArgs, build_helpers, format_result, make_team_tool, run_team_job, team_request)
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
        assert runner.max_iterations == HELPER_STEPS


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


def test_a_job_without_steps_says_why_on_the_first_line():
    from types import SimpleNamespace
    result = SimpleNamespace(outcome="stuck", subtasks=[], content="Supervisor failed to produce a valid plan.")
    assert format_result(result).splitlines()[0] == ("Team job finished (outcome: stuck): "
                                                     "Supervisor failed to produce a valid plan.")


def test_the_tool_description_says_auto_mode_skips_the_question():
    assert "The user is asked before it starts unless auto mode is on." in team_module.TOOL_DESCRIPTION


def test_a_helper_whose_run_raises_gives_the_supervisor_only_the_redacted_text(tools):
    helpers = build_helpers(router(agent=lambda messages: (_ for _ in ()).throw(RuntimeError("key=SECRET123456"))),
                            tools, "", describe_error=lambda e: str(e).replace("SECRET123456", "[hidden]"))
    _, runner = helpers["explorer"]
    with pytest.raises(RuntimeError) as caught:
        runner.Initialize("look")
    assert "SECRET123456" not in str(caught.value) and "[hidden]" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_helper_raising_ctrl_c_or_exit_is_not_rewritten(tools):
    for error in (KeyboardInterrupt, SystemExit):
        helpers = build_helpers(router(agent=lambda messages, e=error: (_ for _ in ()).throw(e())), tools, "")
        with pytest.raises(error):
            helpers["explorer"][1].Initialize("look")


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
    assert result == ("Team job failed (RuntimeError): <boom> " + team_module.MAY_HAVE_CHANGED) and FakeSupervisor.closed


def test_a_supervisor_that_ends_without_a_result_says_so(tmp_path, tools, monkeypatch):
    FakeSupervisor.behaviour = staticmethod(lambda: iter([{"type": "plan_start"}]))
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)
    result, _ = run(tmp_path, tools, router())
    assert result == "Team job ended without a result. " + team_module.MAY_HAVE_CHANGED


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
    assert built["spawn_config"].enabled is False
    assert "memory" not in built and "persistence" not in built
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


CHANGED = "Some steps may already have changed files."


def failing_supervisor(monkeypatch, error):
    def behaviour():
        raise error
        yield  # pragma: no cover

    FakeSupervisor.behaviour = staticmethod(behaviour)
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)


def test_a_failure_result_is_one_short_line_that_ends_with_the_warning(tmp_path, tools, monkeypatch):
    failing_supervisor(monkeypatch, RuntimeError("line one\n" + ("word " * 1000) + "\nlast line"))
    result, _ = run(tmp_path, tools, router())
    assert "\n" not in result and len(result) < 400 + len(CHANGED)
    assert result.startswith("Team job failed (RuntimeError): line one word") and result.endswith(CHANGED)


def test_a_describe_error_that_raises_still_gives_a_result(tmp_path, tools, monkeypatch):
    failing_supervisor(monkeypatch, RuntimeError("boom"))

    def broken(error):
        raise ValueError("describing failed")

    result, _ = run(tmp_path, tools, router(), describe_error=broken)
    assert result == "Team job failed (RuntimeError). " + CHANGED


def test_a_success_result_has_no_changed_files_warning(tmp_path, tools):
    model = router(plans=[plan_json(step("s1", "explorer", "look"))], synth="All done.",
                   agent=lambda messages: make_final("ok"))
    result, _ = run(tmp_path, tools, model)
    assert CHANGED not in result


def test_the_stream_is_closed_when_the_consumer_side_raises(tmp_path, tools, monkeypatch):
    def behaviour():
        yield {"type": "plan", "plan": [{"agent": "explorer", "query": "q"}]}
        yield {"type": "dispatch", "agent": "explorer", "query": "q"}   # never reached

    def interrupt(kind, data):
        raise KeyboardInterrupt

    FakeSupervisor.behaviour = staticmethod(behaviour)
    monkeypatch.setattr(team_module, "Supervisor", FakeSupervisor)
    # `info` keeps the traceback, and with it the generator, alive: only an explicit close() can set `closed`
    with pytest.raises(KeyboardInterrupt) as info:
        run(tmp_path, tools, router(), notify=interrupt)
    assert info.value is not None and FakeSupervisor.closed


def test_a_supervisor_that_cannot_be_built_gives_a_failure_result(tmp_path, tools, monkeypatch):
    class Broken:
        def __init__(self, **kw):
            raise ValueError("bad agents")

    monkeypatch.setattr(team_module, "Supervisor", Broken)
    result, _ = run(tmp_path, tools, router())
    assert result == "Team job failed (ValueError): bad agents " + CHANGED


def test_a_notify_that_raises_does_not_abort_the_job(tmp_path, tools):
    def broken(kind, data):
        raise RuntimeError("display failed")

    model = router(plans=[plan_json(step("s1", "explorer", "look around"))], synth="All done.",
                   agent=lambda messages: make_final("found things"))
    result, _ = run(tmp_path, tools, model, notify=broken)
    assert result.startswith("Team job finished (outcome: done).") and "All done." in result


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


def test_with_extra_models_every_helper_names_its_model_the_chat_model_too(tools):
    chat = labelled("claude-sonnet-5-5")
    opus, gpt = labelled("claude-opus-5-5"), labelled("gpt-5.5")
    helpers = build_helpers(chat, tools, "BASE PROMPT",
                            extra_models=[HelperModel("claude-opus-5-5", opus), HelperModel("gpt-5.5", gpt)])
    assert list(helpers) == ["explorer@claude-sonnet-5-5", "coder@claude-sonnet-5-5", "runner@claude-sonnet-5-5",
                             "explorer@claude-opus-5-5", "coder@claude-opus-5-5", "runner@claude-opus-5-5",
                             "explorer@gpt-5.5", "coder@gpt-5.5", "runner@gpt-5.5"]
    assert helpers["coder@claude-sonnet-5-5"][1].model is chat and helpers["coder@claude-opus-5-5"][1].model is opus
    assert helpers["runner@gpt-5.5"][1].model is gpt
    for name, (description, runner) in helpers.items():
        role, model = name.split("@")
        assert {t.name for t in runner.tools} == role_tools(role)
        assert f"{ROLE_MARK} you are the {role} helper" in runner.system_addendum
        assert "BASE PROMPT" in runner.system_addendum and runner.max_iterations == HELPER_STEPS
        assert f"Runs on {model}." in description and "one copy of each role per model" in description


def test_no_model_is_favoured_in_the_descriptions(tools):
    chat = labelled("claude-sonnet-5-5")
    helpers = build_helpers(chat, tools, "", extra_models=[HelperModel("claude-opus-5-5", labelled("x"))])
    plain = helpers["coder@claude-sonnet-5-5"][0].replace("claude-sonnet-5-5", "M")
    extra = helpers["coder@claude-opus-5-5"][0].replace("claude-opus-5-5", "M")
    assert plain == extra                                        # the same words for every model
    for text in ("unless a step clearly needs", "the chat's model", "harder reasoning"):
        assert text not in helpers["coder@claude-sonnet-5-5"][0] + helpers["coder@claude-opus-5-5"][0]


def test_a_note_on_what_a_model_is_for_reaches_its_helpers_only(tools):
    helpers = build_helpers(labelled("claude-sonnet-5-5"), tools, "", chat_use="everyday work",
                            extra_models=[HelperModel("gpt-6.1-sol", labelled("x"), "hard coding"),
                                          HelperModel("gpt-5.5", labelled("y"))])
    assert "Good for: hard coding." in helpers["coder@gpt-6.1-sol"][0]
    assert "Good for: everyday work." in helpers["runner@claude-sonnet-5-5"][0]
    assert "Good for" not in helpers["coder@gpt-5.5"][0]


def test_helpers_get_as_many_steps_as_the_chat():
    assert HELPER_STEPS == 50


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
    chat = router(plans=[plan_json(step("s1", "explorer@claude-sonnet-5-5", "look"))], synth="Done.")
    chat.model_name = "claude-sonnet-5-5"
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


def test_a_note_ending_in_a_full_stop_is_not_doubled(tools):
    helpers = build_helpers(labelled("claude-sonnet-5-5"), tools, "",
                            extra_models=[HelperModel("gpt-5.5", labelled("y"), "hard coding.")])
    assert "Good for: hard coding." in helpers["coder@gpt-5.5"][0] and ".." not in helpers["coder@gpt-5.5"][0]
