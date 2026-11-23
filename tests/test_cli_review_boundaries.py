"""Regressions for the model-facing and persisted team boundaries."""
import pytest

from agentx_dev.cli import team
from agentx_dev.cli.team_record import TeamRecord
from tests.test_cli_blind_runtime import runtime
from tests.subagent_helpers import PLANNER_MARK, plan_json, step
from tests.conftest import make_final


@pytest.mark.parametrize("streaming", [False, True])
def test_job_failure_hides_unknown_identity_and_exception_type(tmp_path, monkeypatch, streaming):
    primary, _, models, _, tools = runtime(tmp_path)
    record = TeamRecord.for_project(tmp_path, "inspect")
    events = []

    class ProviderDiagnosticError(Exception):
        pass

    diagnostic = "provider-diagnostic rejected unrevealed-model-99"

    def broken(messages):
        raise ProviderDiagnosticError(diagnostic)

    if streaming:
        async def broken_stream(self, *args, **kwargs):
            raise ProviderDiagnosticError(diagnostic)
            yield ""
        monkeypatch.setattr(team.TeamSupervisor, "astream", broken_stream)
    else:
        async def broken_plan(self, *args, **kwargs):
            raise ProviderDiagnosticError(diagnostic)
        monkeypatch.setattr(team.TeamSupervisor, "_plan", broken_plan)

    text = team.run_team_job(
        "inspect", model=primary, tools=tools, extra_models=models, addendum="", record=record,
        notify=lambda kind, data: events.append((kind, data)),
    )
    saved = "\n".join(p.read_text(encoding="utf-8") for p in record.folder.rglob("*.md"))
    for output in (text, saved):
        for forbidden in (diagnostic, "provider-diagnostic", "unrevealed-model-99", "ProviderDiagnosticError"):
            assert forbidden not in output
    assert "failed" in text.lower()
    assert any(diagnostic in str(data) for kind, data in events if kind == "error")


def test_records_preserve_safe_alias_without_model_display_label(tmp_path):
    primary, alternate, models, _, tools = runtime(tmp_path)

    def script(messages):
        if PLANNER_MARK in str(messages[0]["content"]):
            import re
            helper = re.search(r"explorer@model_[a-z0-9]+", str(messages[0]["content"])).group(0)
            return plan_json(step("s1", helper, "inspect", **{"as": "pricing-reader"}))   # the field the planner is told to use
        if "Team role:" in str(messages[0]["content"]):
            return make_final("inspected")
        return "Done."

    primary._script_fn = alternate._script_fn = script
    record = TeamRecord.for_project(tmp_path, "inspect")
    text = team.run_team_job("inspect", model=primary, tools=tools, extra_models=models, addendum="", record=record,
                             notify=lambda *_: None)
    assert "failed" not in text.lower(), text
    assert "pricing-reader" in (record.folder / "job.md").read_text(encoding="utf-8")
    report = record.folder / "01-pricing-reader.md"
    assert report.exists()
    assert "pricing-reader" in report.read_text(encoding="utf-8")
    for path in record.folder.rglob("*.md"):
        saved = path.read_text(encoding="utf-8")
        assert "private-primary" not in saved and "private-alternate" not in saved
