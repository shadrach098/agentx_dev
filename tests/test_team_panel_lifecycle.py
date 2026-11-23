"""Regression coverage for panel identity and conversation boundaries."""
import pytest

from agentx_dev.cli.team_panel import TeamPanel
from tests.test_cli_app import Lines, make_app
from tests.test_cli_team_app import team_model
from tests.subagent_helpers import step


def test_ambiguous_result_never_guesses_first_helper():
    panel = TeamPanel()
    panel.notify("plan", {"round": 1, "steps": [
        {"agent": "a", "query": "same"}, {"agent": "a", "query": "same"}]})
    panel.notify("result", {"agent": "a", "round": 1, "step": 1, "skipped": True})
    assert [r["state"] for r in panel.snapshot()] == ["waiting", "skipped"]
    panel.notify("result", {"agent": "a", "ok": True})
    assert [r["state"] for r in panel.snapshot()] == ["waiting", "skipped"]


def test_old_round_results_do_not_touch_reused_slots():
    panel = TeamPanel()
    for round_no in (1, 2):
        panel.notify("plan", {"round": round_no, "steps": [{"agent": "a", "query": "same"}]})
    panel.notify("result", {"round": 1, "step": 0, "agent": "a", "ok": True})
    assert [r["state"] for r in panel.snapshot()] == ["done", "waiting"]


def test_teardown_does_not_invent_helper_failure():
    panel = TeamPanel()
    panel.notify("plan", {"steps": [{"agent": "a", "query": "q"}]})
    panel.finish("interrupted")
    row = panel.snapshot()[0]
    assert row["state"] == "interrupted"
    assert row["termination"] == "interrupted"
    assert row["inferred"] is True


def test_clear_clears_the_panel(tmp_path):
    model = team_model([step("s1", "explorer", "look")])
    app, _, _, _ = make_app(tmp_path, Lines("do job", "/clear", "/exit"), model, mode="auto")
    app.start()
    assert app._team_panel.snapshot() == []


def test_resuming_another_conversation_clears_the_panel(tmp_path):
    from agentx_dev.cli.sessions import list_sessions
    model = team_model([step("s1", "explorer", "look")])
    app, _, _, home = make_app(tmp_path, Lines("do job", "/exit"), model, mode="auto")
    app.start()
    assert app._team_panel.snapshot()                  # the job's rows stay until the conversation changes
    saved = list_sessions(home, app.cwd)[0].session_id[:8]
    app.resume(saved)                                  # /resume <id> (there is no "/resume last")
    assert app._team_panel.snapshot() == []


def test_producer_callbacks_carry_round_and_step_identity(tmp_path):
    from agentx_dev.cli.team import run_team_job
    from agentx_dev.cli.tools import build_tools
    from agentx_dev.cli.approval import Gate

    events = []
    model = team_model([step("s1", "explorer", "same"), step("s2", "explorer", "same")])
    run_team_job("inspect", model=model, tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)),
                 addendum="", notify=lambda kind, data: events.append((kind, data)))
    plans = [d for k, d in events if k == "plan"]
    assert plans[0]["round"] == 1
    assert [s["step_id"] for s in plans[0]["steps"]] == ["s1", "s2"]
    # Reading steps run side by side, so they may start and end in either order: each one is named exactly once,
    # and a result carries the identity its own start had.
    started = {}
    for kind in ("dispatch", "result"):
        emitted = [d for k, d in events if k == kind]
        assert sorted((d["round"], d["step"], d["step_id"]) for d in emitted) == [(1, 0, "s1"), (1, 1, "s2")]
        for d in emitted:
            ident = (d["round"], d["step"], d["step_id"])
            if kind == "dispatch":
                started[id(d["status"])] = ident
            else:
                assert started[id(d["status"])] == ident


def test_last_tool_is_not_in_flight_activity():
    from agentx_dev.cli.team import StepStatus
    panel = TeamPanel()
    panel.notify("plan", {"round": 1, "steps": [{"agent": "a"}]})
    status = StepStatus(0, "a", "task", "query")
    panel.notify("dispatch", {"round": 1, "step": 0, "status": status})
    status.note_call("read_path", {"path": "x.py"}, "contents")
    row = panel.snapshot()[0]
    assert row["action"] == "read_path"
    assert row["inFlight"] is False
    status.begin_call("edit_file", {"path": "x.py"})
    row = panel.snapshot()[0]
    assert row["action"] == "edit_file" and row["inFlight"] is True
    status.note_call("edit_file", {"path": "x.py"}, "Error: denied")
    row = panel.snapshot()[0]
    assert row["inFlight"] is False and row["actionFailed"] is True
    assert "Updated" not in row["summary"]


def test_unknown_teardown_is_not_a_failure_or_skip():
    panel = TeamPanel()
    panel.notify("plan", {"steps": [{"agent": "a"}]})
    panel.finish()
    row = panel.snapshot()[0]
    assert row["state"] == "unknown"
    assert row["termination"] == "unknown" and row["inferred"] is True
