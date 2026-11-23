"""Live notes use supervisor-only safe boundaries, never helper interjections."""
import json

from agentx_dev.cli import team
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.tools import build_tools
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import PLANNER_MARK, plan_json, step


def run_notes(tmp_path, decision, *, closed_after_note=False):
    seen = {"helpers": [], "triage": [], "readers": [], "closed": False, "finished": 0}

    def script(messages):
        system = str(messages[0]["content"])
        if PLANNER_MARK in system:
            return plan_json(step("initial", "explorer", "inspect original scope"))
        if "LIVE_NOTE_TRIAGE" in system:
            seen["triage"].append(str(messages))
            assert seen["finished"] == 1  # the original helper returned its final reply
            return json.dumps(decision)
        if "Team role:" in system:
            seen["helpers"].append(str(messages))
            if len(seen["helpers"]) == 1:
                team._active_job.add_note("PRIVATE NOTE: verify the extra file", seen["readers"].append)
                seen["closed"] = closed_after_note
                # The runner polls interjections again before its next model call.
                from tests.conftest import make_react_response
                return make_react_response("list_directory", {"path": "."})
            seen["finished"] += 1
            return make_final("checked")
        return "Summary."

    result = team.run_team_job(
        "inspect the project", model=MockModel(script=script),
        tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)), addendum="",
        notify=lambda *_: None, closed=lambda: seen["closed"],
    )
    return result, seen


def test_note_is_triaged_after_helper_finishes_then_routed_without_raw_injection(tmp_path):
    result, seen = run_notes(tmp_path, {"action": "route", "reason": "Check requested scope",
                                     "steps": [{"agent": "explorer", "query": "Inspect extra.txt"}]})
    assert seen["triage"]
    assert seen["readers"] == ["supervisor"]
    assert all("PRIVATE NOTE" not in messages for messages in seen["helpers"])
    assert any("Inspect extra.txt" in messages for messages in seen["helpers"])
    assert "PRIVATE NOTE" not in result


def test_invalid_route_is_not_dispatched_and_note_remains_in_partial_handoff(tmp_path):
    result, seen = run_notes(tmp_path, {"action": "route", "reason": "Unknown target",
                                     "steps": [{"agent": "unregistered", "query": "Do it"}]})
    assert seen["triage"]
    assert not any("Do it" in messages for messages in seen["helpers"])
    assert "PRIVATE NOTE" in result
    assert "pending" in result.lower()


def test_wrap_up_does_not_triage_or_route_notes(tmp_path):
    result, seen = run_notes(tmp_path, {"action": "route", "reason": "More work",
                                     "steps": [{"agent": "explorer", "query": "Inspect extra.txt"}]},
                             closed_after_note=True)
    assert not seen["triage"]
    assert "PRIVATE NOTE" in result
    assert not any("Inspect extra.txt" in messages for messages in seen["helpers"])
