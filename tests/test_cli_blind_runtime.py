"""Opaque model IDs stay intact through the real CLI team runtime."""
import asyncio
import json

import pytest

from agentx_dev import AgentSpec
from agentx_dev.cli import team
from agentx_dev.cli.team_record import TeamRecord
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.blind_models import BlindModelRoster
from agentx_dev.cli.tools import build_tools
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import PLANNER_MARK, plan_json, step, new_agent_step


def runtime(tmp_path, extras=True):
    primary = MockModel()
    primary.model = "private-primary"
    alternate = MockModel()
    alternate.model = "private-alternate"
    models = [team.HelperModel("private-alternate", alternate, "private-alternate for checks")] if extras else []
    roster = BlindModelRoster(primary, models)
    tools = build_tools(tmp_path, [], Gate(cwd=tmp_path))
    return primary, alternate, models, roster, tools


def supervisor(primary, helpers, roster, **kwargs):
    return team.TeamSupervisor(
        model=primary, agents=helpers, verbose=False,
        closed=lambda: False, roster=roster, **kwargs,
    )


@pytest.mark.parametrize("extras", [False, True])
def test_helpers_use_opaque_ids_and_keep_raw_models_private(tmp_path, extras):
    primary, alternate, models, roster, tools = runtime(tmp_path, extras)
    helpers = team.build_helpers(primary, tools, "", extra_models=models, roster=roster)
    assert len(helpers) == (6 if extras else 3)
    assert all("private-" not in name + description for name, (description, _) in helpers.items())
    if extras:
        assert all(any(key in name for key, _ in roster.choices) for name in helpers)
    else:
        assert set(helpers) == {"explorer", "coder", "runner"}



def test_supervisor_validates_ids_and_human_labels_are_resolved(tmp_path):
    primary, alternate, models, roster, tools = runtime(tmp_path)
    sup = supervisor(primary, team.build_helpers(primary, tools, "", extra_models=models, roster=roster), roster)
    selected = next(key for key, _ in roster.choices if roster.resolve(key) is alternate)
    assert sup._allowed_model(selected) == selected
    assert sup._host_model(selected) == "private-alternate"
    with pytest.raises(ValueError):
        sup._allowed_model("private-alternate")


def test_planner_and_replanner_catalog_hide_model_identity(tmp_path):
    primary, _, models, roster, tools = runtime(tmp_path)
    prompts = []

    def script(messages):
        prompts.append(str(messages))
        return plan_json(step("inspect", "explorer@" + roster.choices[0][0], "inspect files"))

    primary._script_fn = script
    sup = supervisor(primary, team.build_helpers(primary, tools, "", extra_models=models, roster=roster), roster)

    async def plan_twice():
        assert await sup._plan("inspect files")
        assert await sup._plan_recovery("inspect files", [], 1)

    asyncio.run(plan_twice())
    assert len(prompts) == 2
    for prompt in prompts:
        assert "private-primary" not in prompt
        assert "private-alternate" not in prompt
        assert all(key in prompt for key, _ in roster.choices)


def test_dynamic_dispatch_passes_opaque_id_to_closure(tmp_path):
    primary, alternate, models, roster, tools = runtime(tmp_path)
    selected = next(key for key, _ in roster.choices if roster.resolve(key) is alternate)
    helpers = team.build_helpers(primary, tools, "", extra_models=models, roster=roster)
    received = []

    def make_helper(name, instructions, tool_names, model_id):
        received.append(model_id)
        assert roster.resolve(model_id) is alternate
        return team.build_helpers(alternate, tools, "")["explorer"][1]

    sup = supervisor(primary, helpers, roster, make_helper=make_helper)
    name, error = sup.spawn_specialist(AgentSpec(name="checker", instructions="Check files", model=selected))
    assert (name, error) == ("checker", "")
    assert received == [selected]
    assert "checker" in sup.agents


@pytest.mark.parametrize("reuse", [False, True])
def test_dynamic_dispatch_rejects_invalid_id_before_reuse_or_constructor(tmp_path, reuse):
    primary, _, models, roster, tools = runtime(tmp_path)
    helpers = team.build_helpers(primary, tools, "", extra_models=models, roster=roster)
    received = []

    def fail_constructor(*args):
        received.append(args)
        raise RuntimeError("constructor must not run")

    sup = supervisor(primary, helpers, roster, make_helper=fail_constructor)
    if reuse:
        sup._new_helpers["checker"] = True
    name, error = sup.spawn_specialist(AgentSpec(
        name="checker", instructions="Check files", model="private-primary",
    ))
    assert name is None
    assert "model" in error.lower()
    assert received == []
    assert "checker" not in sup.agents


def test_valid_id_constructor_exception_is_contained_without_identity_leak(tmp_path):
    primary, alternate, models, roster, tools = runtime(tmp_path)
    selected = next(key for key, _ in roster.choices if roster.resolve(key) is alternate)
    received = []

    def broken_constructor(name, instructions, tool_names, model_id):
        received.append(model_id)
        raise RuntimeError("private-alternate provider failed")

    sup = supervisor(primary, team.build_helpers(primary, tools, "", extra_models=models, roster=roster),
                     roster, make_helper=broken_constructor)
    name, error = sup.spawn_specialist(AgentSpec(
        name="checker", instructions="Check files", model=selected,
    ))
    assert name is None and error == "could not build it (RuntimeError)"
    assert "private-alternate" not in error and "provider failed" not in error
    assert received == [selected]
    assert "checker" not in sup.agents and "checker" not in sup._new_helpers


@pytest.mark.parametrize("bad_id", [False, True])
def test_consume_shares_one_roster_and_resolves_dynamic_models(tmp_path, monkeypatch, bad_id):
    seen = {"rosters": [], "helper_calls": 0, "plans": 0}

    def primary_script(messages):
        system = str(messages[0]["content"])
        if PLANNER_MARK in system:
            seen["plans"] += 1
            if seen["plans"] == 1:
                roster = seen["rosters"][0]
                selected = next(key for key, _ in roster.choices if roster.resolve(key) is alternate)
                return plan_json(new_agent_step("check", "checker", "Check files", query="inspect files",
                    new_agent={"name": "checker", "instructions": "Check files", "tools": [],
                               "model": "private-alternate" if bad_id else selected}))
            return plan_json(step("fallback", "explorer", "inspect files"))
        if "Team role:" in system:
            return make_final("checked by primary")
        return "Summary."

    def alternate_script(messages):
        seen["helper_calls"] += 1
        return make_final("checked by alternate")

    primary = MockModel(script=primary_script)
    primary.model = "private-primary"
    alternate = MockModel(script=alternate_script)
    alternate.model = "private-alternate"
    real_roster = team.BlindModelRoster

    def track_roster(*args, **kwargs):
        roster = real_roster(*args, **kwargs)
        seen["rosters"].append(roster)
        return roster

    monkeypatch.setattr(team, "BlindModelRoster", track_roster)
    result = team.run_team_job(
        "inspect files", model=primary, extra_models=[team.HelperModel("private-alternate", alternate, "checks")],
        tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)), addendum="", notify=lambda *_: None,
    )
    assert "Team job failed" not in result
    assert len(seen["rosters"]) == 1
    assert seen["helper_calls"] == (0 if bad_id else 1)


def test_single_model_dynamic_helper_can_select_an_id_from_its_prompt():
    model = MockModel()
    model.model = "private-primary"
    roster = BlindModelRoster(model)
    built = []
    sup = team.TeamSupervisor(model=model, agents={}, verbose=False, roster=roster,
                          spawn_config=team.SpawnConfig(enabled=True),
                          make_helper=lambda *args: built.append(args) or object())
    prompt = sup._build_agent_catalog()
    opaque_id = roster.choices[0][0]
    assert opaque_id in prompt
    assert "private-primary" not in prompt
    name, error = sup._spawn_spec(AgentSpec("reader", "Read", (), model=opaque_id))
    assert name == "reader" and not error and built


def test_primary_capability_comment_survives_runtime_sanitization(tmp_path):
    from agentx_dev.cli.approval import Gate
    from agentx_dev.cli.tools import build_tools
    from agentx_dev.cli.team import run_team_job
    from tests.subagent_helpers import router, plan_json, step
    model = router(plans=[plan_json(step("a", "explorer", "read"))], synth="Done")
    model.model_name = "private-primary"
    run_team_job("read", model=model, tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)),
                 addendum="", notify=lambda *args: None,
                 chat_use="private-primary for careful reading")
    prompt = model.planner_prompts()[0]
    assert "careful reading" in prompt
    assert "private-primary" not in prompt


@pytest.mark.parametrize("recover", [False, True])
def test_helper_provider_error_is_blind_in_recovery_results_and_records(tmp_path, monkeypatch, recover):
    from agentx_dev.Runner.Persistence import Persistence

    original_init = team.TeamSupervisor.__init__

    def configured_init(self, *args, **kwargs):
        kwargs["persistence"] = Persistence(max_replans=1, patient_retries=False)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(team.TeamSupervisor, "__init__", configured_init)
    primary, alternate, models, roster, tools = runtime(tmp_path)
    prompts = []
    plans = 0

    def script(messages):
        nonlocal plans
        system = str(messages[0]["content"])
        prompts.append(str(messages))
        if PLANNER_MARK in system:
            plans += 1
            if plans == 1:
                import re
                selected = re.search(r"explorer@(model_[a-z0-9]+)", system)
                # Select either advertised helper: both fail on their first execution.
                helper = selected.group(0) if selected else "explorer"
                return plan_json(step("s1", helper, "inspect"))
            if recover:
                import re
                helper = re.search(r"explorer@model_[a-z0-9]+", system).group(0)
                return plan_json(step("s2", helper, "try another approach", replaces=["s1"]))
            return plan_json()
        if "Team role:" in system:
            if plans == 1:
                raise RuntimeError("private-primary provider-a rejected SECRET123456")
            return make_final("inspected")
        return "Inspection complete."

    primary._script_fn = script
    alternate._script_fn = script
    events = []
    text = team.run_team_job(
        "inspect", model=primary, tools=tools, extra_models=models, addendum="", record=TeamRecord.for_project(tmp_path, "inspect"),
        describe_error=lambda error: str(error).replace("SECRET123456", "[hidden]"),
        notify=lambda kind, data: events.append((kind, data)),
    )
    recovery = [p for p in prompts if "RECOVERY ROUND" in p]
    assert recovery, (text, prompts, events)
    for name in ("private-primary", "provider-a", "SECRET123456"):
        assert name not in text + "\n".join(recovery)
    human_errors = [data["text"] for kind, data in events if kind == "result" and not data["ok"]]
    assert human_errors and "private-primary" in human_errors[0]
    assert "[hidden]" in human_errors[0] and "SECRET123456" not in human_errors[0]
    files = [p for p in (tmp_path / ".agentx" / "team").rglob("*") if p.is_file()]
    assert files
    for path in files:
        saved = path.read_text(encoding="utf-8")
        assert "private-primary" not in saved, path
        assert "provider-a" not in saved, path


def test_chooser_provider_error_is_blind_but_human_notification_retains_diagnostics(tmp_path):
    primary, _, models, _, tools = runtime(tmp_path)

    def provider_error(messages):
        raise RuntimeError("private-primary provider-b unavailable")

    primary._script_fn = provider_error
    events = []
    text = team.run_team_job(
        "inspect", model=primary, tools=tools, extra_models=models, addendum="", record=TeamRecord.for_project(tmp_path, "inspect"),
        notify=lambda kind, data: events.append((kind, data)),
    )
    assert "private-primary" not in text and "provider-b" not in text
    assert any("private-primary" in str(data) for kind, data in events if kind == "error")
    for path in (tmp_path / ".agentx" / "team").rglob("*"):
        if path.is_file():
            assert "private-primary" not in path.read_text(encoding="utf-8"), path


def test_recorded_job_keeps_human_labels_out_of_recommended_files(tmp_path):
    primary, alternate, models, _, tools = runtime(tmp_path)

    selected = []

    def script(messages, label):
        system = str(messages[0]["content"])
        if PLANNER_MARK in system:
            import re
            helper = re.search(r"explorer@model_[a-z0-9]+", system).group(0)
            return plan_json(step("s1", helper, "inspect"))
        if "Team role:" in system:
            selected.append(label)
            return make_final("inspected")
        return "Done."

    primary._script_fn = lambda messages: script(messages, "private-primary")
    alternate._script_fn = lambda messages: script(messages, "private-alternate")
    events = []
    text = team.run_team_job(
        "inspect", model=primary, tools=tools, extra_models=models, addendum="", record=TeamRecord.for_project(tmp_path, "inspect"),
        notify=lambda kind, data: events.append((kind, data)),
    )
    assert "job.md" in text and "report" in text.lower()
    assert selected
    assert any(selected[0] in str(data) for kind, data in events if kind == "dispatch"), (text, events)
    files = [p for p in (tmp_path / ".agentx" / "team").rglob("*") if p.is_file()]
    assert any(p.name == "job.md" for p in files)
    for path in files:
        saved = path.read_text(encoding="utf-8")
        assert "private-primary" not in saved, path
        assert "private-alternate" not in saved, path


@pytest.mark.parametrize("error, safe_reason", [
    ("unknown model ID", True),
    ("spawn refused: unknown model ID", True),
    ("unknown model ID: private-primary", False),
    ("spawn refused: unknown model ID: private-primary", False),
    ("provider-a failed: unknown model ID", False),
])
def test_failed_step_formatter_only_preserves_allowlisted_invalid_id_errors(error, safe_reason):
    from types import SimpleNamespace

    failed = SimpleNamespace(agent="slot-01", query="inspect", content="", skipped=False,
                             error=error, superseded=False)
    text = team.format_result(SimpleNamespace(outcome="stuck", subtasks=[failed], content="Not finished."))
    assert ("unknown model ID" in text) is safe_reason
    assert ("Helper execution failed." in text) is not safe_reason
    assert "private-primary" not in text and "provider-a" not in text


def test_failed_step_formatter_does_not_repeat_provider_identity():
    from types import SimpleNamespace

    failed = SimpleNamespace(agent="slot-01", query="inspect", content="", skipped=False,
                             error="provider-a private-primary failed", superseded=False)
    text = team.format_result(SimpleNamespace(outcome="partial", subtasks=[failed], content="Not finished."))
    assert "slot-01 - failed" in text and "Not finished." in text
    assert "private-primary" not in text and "provider-a" not in text


def test_a_new_helper_without_a_model_runs_on_the_chat_model(tmp_path):
    """The planner is told a new helper without "model" runs on the chat's model. With one model (no extra team
    models, the usual case) every planner-made helper was refused as "unknown model ID" and the job did nothing."""
    from agentx_dev.cli.approval import Gate
    from agentx_dev.cli.tools import build_tools
    from tests.conftest import MockModel
    from tests.subagent_helpers import new_agent_step
    ran = []

    def script(messages):
        system = str(messages[0]["content"])
        if PLANNER_MARK in system:
            return plan_json(new_agent_step("s1", "fixer", "Find the bug.", tools=["read_path"], query="do it"))
        if "you are the fixer" in system.lower():
            ran.append(1)
            return make_final("fixed")
        return "Summary."

    text = team.run_team_job("job", model=MockModel(script=script), tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)),
                             addendum="", notify=lambda *_: None)
    assert ran == [1] and "unknown model ID" not in text and "fixer - done" in text
