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


# -- the window's live team block: each row carries what it shows ----------------------------------------------

def _terminal_lines(agent, label, ok, text, status=None, **kw):
    """What the terminal prints for a step that ended (``Screen.team_done``), as plain text."""
    import io
    from rich.console import Console
    from agentx_dev.cli.ui import Screen
    out = io.StringIO()
    Screen(Console(file=out, width=200, force_terminal=False, color_system=None, legacy_windows=False)).team_done(
        agent, label, ok, text, status=status, **kw)
    return out.getvalue()


def test_each_panel_is_its_own_job():
    assert TeamPanel().id != TeamPanel().id and TeamPanel().id


def test_an_ended_row_reads_exactly_as_the_terminal_ended_line():
    """The block shows a step that ended the way the terminal does: mark, helper · label, the word, its numbers,
    and the start of its report under it."""
    panel = TeamPanel()
    plan(panel, 1, step(), step("reviewer", "Review", "review"), step("writer", "Write", "write"),
         step("checker", "Check", "check"), step("fixer", "Fix", "fix"))
    cases = [(0, "builder", {"ok": True, "text": "## Built\nThe **builder** made app.js."}),
             (1, "reviewer", {"ok": False, "text": "The provider refused."}),
             (2, "writer", {"ok": True, "partial": True, "text": "Half of it."}),
             (3, "checker", {"ok": True, "skipped": True, "text": "Not needed."}),
             (4, "fixer", {"ok": True, "text": "", "fallback": "qwen3:8b", "fallback_from": "claude-opus-5-5",
                           "fallback_reason": "out of credit"})]
    for index, agent, kw in cases:
        status = dispatch(panel, 1, index, agent=agent, slot=index)
        status.note_call("read_path", {"path": "a.py"}, "contents")
        status.ended = status.started + 16
        result(panel, 1, index, status, agent=agent, **kw)
    rows = panel.snapshot()
    assert [r["mark"] for r in rows] == ["✓", "✗", "◐", "–", "↺"]
    assert rows[0]["report"] == "Built The builder made app.js."
    assert rows[4]["report"] == "(no report)"
    for (index, agent, kw), row in zip(cases, rows):
        printed = _terminal_lines(agent, row["label"], kw["ok"], kw["text"], status=panel.rows[index]["status"],
                                  partial=kw.get("partial", False), skipped=kw.get("skipped", False),
                                  fallback={"to": kw.get("fallback"), "from": kw.get("fallback_from"),
                                            "reason": kw.get("fallback_reason")})
        assert f"{row['mark']} {row['helper']} · {row['label']}  {row['word']} ({row['stats']})" in printed, printed
        assert f"⎿ {row['report']}" in printed
    assert rows[4]["word"] == "Done on qwen3:8b (fallback: claude-opus-5-5 out of credit)"
    assert [r["tone"] for r in rows] == ["green", "red", "yellow", "dim", "yellow"]


def test_a_row_that_never_ended_has_no_ended_line_and_a_stopped_job_says_so():
    panel = TeamPanel()
    plan(panel, 1, step(), step("reviewer", "Review", "review"))
    dispatch(panel, 1, 0)
    rows = panel.snapshot()
    assert [r["mark"] for r in rows] == ["", ""] and [r["report"] for r in rows] == ["", ""]
    panel.finish("interrupted")
    assert [(r["mark"], r["word"]) for r in panel.snapshot()] == [("■", "Stopped"), ("■", "Stopped")]
    unknown = TeamPanel()
    plan(unknown, 1, step())
    unknown.finish()
    assert (unknown.snapshot()[0]["mark"], unknown.snapshot()[0]["word"]) == ("?", "Ended without word")


def test_a_running_row_carries_its_tool_count_time_and_last_actions():
    panel = TeamPanel()
    plan(panel, 1, step())
    status = dispatch(panel, 1, 0)
    for n in range(7):
        status.note_call("read_path", {"path": f"f{n}.py"}, "line\n" * (n + 1))
    status.begin_call("grep", {"pattern": "TODO"})
    status.started -= 72
    row = panel.snapshot()[0]
    assert row["toolUses"] == 7 and row["time"] == "1m 12s"
    assert [a["subject"] for a in row["actions"]] == ["f2.py", "f3.py", "f4.py", "f5.py", "f6.py"]
    assert all(a["name"] == "read_path" and not a["failed"] for a in row["actions"])
    assert row["inFlight"] and row["action"] == "grep"


def test_the_record_of_a_job_is_its_rows_in_their_final_form():
    panel = TeamPanel()
    plan(panel, 1, step(), step("reviewer", "Review", "review"))
    status = dispatch(panel, 1, 0)
    result(panel, 1, 0, status, ok=True, text="All built.")
    panel.finish()
    kept = panel.record("Build the thing")
    assert kept["job"] == panel.id and kept["task"] == "Build the thing"
    assert kept["done"] == 1 and kept["total"] == 2
    assert [r["state"] for r in kept["rows"]] == ["done", "unknown"]
    assert kept["rows"][0]["report"] == "All built."
    import json
    json.dumps(kept)                                  # it is kept in the saved conversation
