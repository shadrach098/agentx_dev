"""Persistent, UI-only team deployments; no model calls or selection metadata. The events here have the shape
``run_team_job`` sends: a plan carries its round, a start and a result carry the round, step and step id."""
from agentx_dev.cli.team import StepStatus
from agentx_dev.cli.team_panel import TeamPanel


def plan(panel, round_no, *steps):
    panel.notify("plan", {"round": round_no,
                          "steps": [{**s, "step_id": f"s{n + 1}"} for n, s in enumerate(steps)]})


def step(agent="builder", query="Build", label="implement"):
    return {"agent": agent, "query": query, "label": label}


def dispatch(panel, round_no, index, agent="builder", query="Build", slot=0):
    status = StepStatus(slot, agent, "implement", query)
    status.model = "Human model"
    panel.notify("dispatch", {"agent": agent, "query": query, "status": status,
                              "round": round_no, "step": index, "step_id": f"s{index + 1}"})
    return status


def result(panel, round_no, index, status=None, agent="builder", **kw):
    panel.notify("result", {"agent": agent, "label": "implement", "status": status,
                            "round": round_no, "step": index, "step_id": f"s{index + 1}", **kw})


def test_queued_running_done_and_reconnect_snapshot():
    panel = TeamPanel()
    plan(panel, 1, step(), step("reviewer", "Review", "review"))
    before = panel.snapshot()
    assert [r["state"] for r in before] == ["waiting", "waiting"]
    status = dispatch(panel, 1, 0)
    assert panel.snapshot()[0]["state"] == "running"
    assert panel.snapshot()[0]["model"] == "Human model"
    status.note_call("read_path", {"path": "app.js"}, "contents")
    assert panel.snapshot()[0]["action"] == "read_path"
    assert panel.snapshot()[0]["path"] == "app.js"
    result(panel, 1, 0, status, ok=True)
    after = panel.snapshot()
    assert [r["state"] for r in after] == ["done", "waiting"]
    assert [r["id"] for r in after] == [r["id"] for r in before]
    assert panel.snapshot() == after  # reconnect needs no replay of progress events


def test_parallel_repeated_helpers_replans_failure_and_skip():
    panel = TeamPanel()
    plan(panel, 1, step(), step(query="Other"), step("reviewer", "Review", "review"))
    first = dispatch(panel, 1, 0)
    second = dispatch(panel, 1, 1, query="Other", slot=1)
    assert [r["state"] for r in panel.snapshot()] == ["running", "running", "waiting"]
    result(panel, 1, 1, second, ok=False)
    result(panel, 1, 2, agent="reviewer", skipped=True)
    assert [r["state"] for r in panel.snapshot()] == ["running", "failed", "skipped"]
    old_ids = [r["id"] for r in panel.snapshot()]
    plan(panel, 2, step())
    third = dispatch(panel, 2, 0, slot=0)            # the same helper, slot and query again: a new row
    result(panel, 1, 0, first, ok=True)
    assert [r["state"] for r in panel.snapshot()] == ["done", "failed", "skipped", "running"]
    assert [r["id"] for r in panel.snapshot()][:3] == old_ids
    assert len({r["id"] for r in panel.snapshot()}) == 4
    result(panel, 2, 0, third, ok=True)
    assert panel.snapshot()[3]["state"] == "done"


def test_no_fabricated_activity_and_undispatched_replan_rows_skipped():
    panel = TeamPanel()
    plan(panel, 1, step())
    assert not panel.snapshot()[0]["action"]
    plan(panel, 2, step("reviewer", "Review", "review"))
    assert [r["state"] for r in panel.snapshot()] == ["skipped", "waiting"]
    status = dispatch(panel, 2, 0, "reviewer", "Review")
    assert not panel.snapshot()[1]["action"]
    status.note_call("edit_file", {"path": "app.js"}, "Error: denied")
    row = panel.snapshot()[1]
    assert row["action"] == "edit_file" and row["path"] == "app.js"
    assert row["actionFailed"] is True
    assert "denied" in row["summary"]


def test_a_job_that_ends_with_steps_open_marks_them_unknown_not_failed():
    """The panel was not told how they ended: it says so instead of inventing a failure or a skip."""
    panel = TeamPanel()
    plan(panel, 1, step(), step("reviewer"))
    dispatch(panel, 1, 0)
    panel.finish()
    assert [r["state"] for r in panel.snapshot()] == ["unknown", "unknown"]
    stopped = TeamPanel()
    plan(stopped, 1, step())
    dispatch(stopped, 1, 0)
    stopped.finish("interrupted")                       # Ctrl+C or Esc
    assert stopped.snapshot()[0]["state"] == "interrupted"
