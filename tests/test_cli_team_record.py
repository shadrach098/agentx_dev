"""A team job's plan and every helper's whole report are written to <project>/.agentx/team as they happen, and the
finished reports join the project index, so `recall` finds a helper's research after the terminal closed."""

import threading

from agentx_dev.cli.approval import Gate
from agentx_dev.cli.recall import ProjectMemory
from agentx_dev.cli.team import run_team_job
from agentx_dev.cli.team_record import TeamRecord
from agentx_dev.cli.tools import build_tools
from tests.conftest import make_final
from tests.subagent_helpers import plan_json, router, step

SECRET = "sk-ant-" + "b" * 30


def run(tmp_path, plan, work, record):
    model = router(plans=[plan_json(*plan)], synth="All done.", agent=work)
    tools = build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path))
    return run_team_job("Find the pricing bugs", model=model, tools=tools, addendum="",
                        notify=lambda kind, data: None, record=record)


def test_the_plan_and_each_whole_report_are_written(tmp_path):
    long_report = "Found it: " + "detail " * 400 + "END"           # longer than any one-line summary
    record = TeamRecord.for_project(tmp_path, "Find the pricing bugs")
    text = run(tmp_path, [{**step("a", "explorer", "look at pricing.py"), "as": "pricing-reader"},
                          step("b", "explorer", "look at orders.py")], lambda m: make_final(long_report), record)
    folder = record.folder
    assert folder.parent == tmp_path / ".agentx" / "team"
    job = (folder / "job.md").read_text(encoding="utf-8")
    assert "Find the pricing bugs" in job and "pricing-reader" in job and "## Result" in job
    steps = sorted(p.name for p in folder.glob("0*.md"))
    assert steps == ["01-pricing-reader.md", "02-explorer.md"]
    report = (folder / steps[0]).read_text(encoding="utf-8")
    assert "Status: **done**" in report and "look at pricing.py" in report and report.rstrip().endswith("END")
    assert ".agentx/team/" in text and "read those files" in text            # the main agent is told where


def test_a_step_that_never_finished_keeps_what_it_was_asked(tmp_path):
    record = TeamRecord.for_project(tmp_path, "job")
    started = threading.Event()

    class Status:
        slot, helper, label, query = 1, "coder", "fix", "fix the bug in orders.py"
    record.started(Status)
    started.set()
    text = (record.folder / "01-coder.md").read_text(encoding="utf-8")
    assert "running" in text and "fix the bug in orders.py" in text


def test_secrets_are_redacted(tmp_path):
    record = TeamRecord.for_project(tmp_path, f"use key {SECRET}", redact=lambda t: t.replace(SECRET, "[hidden]"))
    assert SECRET not in (record.folder / "job.md").read_text(encoding="utf-8")


def test_recall_finds_a_finished_report_and_skips_a_running_one(tmp_path):
    record = TeamRecord.for_project(tmp_path, "research")

    class Done:
        slot, helper, label, query, tool_uses, tokens = 1, "web-researcher", "prices", "compare prices", 3, 900
        files, calls = {}, {"web_fetch": 3}

    class Running:
        slot, helper, label, query = 2, "coder", "fix", "change checkout.ts"

    class Result:
        content, error, skipped = "Stripe charges 1.4% plus 20p for European cards.", "", False
    record.finished(Done, Result, ok=True)
    record.started(Running)
    memory = ProjectMemory(tmp_path)
    hits = memory.search("stripe european card fees")
    assert hits and hits[0].path.startswith(".agentx/team/") and hits[0].path.endswith("01-web-researcher.md")
    assert not any("02-coder" in hit.path for hit in memory.search("checkout.ts change"))
    assert "team report" in memory.describe()


def test_a_linked_agentx_folder_is_not_written_through(tmp_path):
    import os
    import pytest
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    try:
        os.symlink(outside, project / ".agentx", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make a folder link")
    assert TeamRecord.for_project(project, "job") is None and not list(outside.iterdir())
