"""The work log kept in a project folder."""

import os
from datetime import datetime

import pytest

from agentx_dev.cli.memory import (MAX_ENTRY_CHARS, TurnRecord, append_entry, entry_text, log_path, read_recent,
                                   record_from_events, summary_entry_text)

WHEN = datetime(2026, 10, 12, 14, 3)


def call(name, args, result="ok", is_error=False):
    return [{"type": "tool_call", "name": name, "args": args},
            {"type": "tool_result", "name": name, "result": result, "is_error": is_error}]


def events(*calls, answer="Done."):
    out = []
    for item in calls:
        out.extend(item)
    out.append({"type": "final", "content": answer})
    return out


def test_changes_commands_and_the_answer_are_taken_from_the_events():
    record = record_from_events("Add docstrings", events(
        call("write_file", {"path": "src/a.py"}, "created: 10 bytes to src/a.py"),
        call("edit_file", {"path": "src/b.py"}, "edited src/b.py: replaced 1 occurrence"),
        call("delete_path", {"path": "old.txt"}, "deleted old.txt"),
        call("move_file", {"source": "x", "destination": "y"}, "moved"),
        call("run_shell", {"command": "pytest -q"}, "3 passed"),
        call("run_python", {"code": "print(1)\nprint(2)"}, "1"),
        call("read_path", {"path": "README.md"}, "text"),
        answer="Added docstrings."))
    assert record.changed == ["src/a.py", "src/b.py", "old.txt", "x -> y"]
    assert record.ran == ["pytest -q", "python: print(1)"]
    assert record.answer == "Added docstrings." and record.request == "Add docstrings"


def test_refused_declined_and_failed_calls_are_not_counted():
    record = record_from_events("x", events(
        call("write_file", {"path": "a"}, "The user declined: no"),
        call("write_file", {"path": "b"}, "Refused: plan mode is on."),
        call("edit_file", {"path": "c"}, "ERROR: substring not found in c"),
        call("run_shell", {"command": "rm x"}, "boom", is_error=True),
        call("write_file", {"path": "d"}, "created: 1 bytes to d")))
    assert record.changed == ["d"] and record.ran == []


def test_a_path_changed_twice_is_listed_once():
    record = record_from_events("x", events(call("edit_file", {"path": "a.py"}), call("edit_file", {"path": "a.py"})))
    assert record.changed == ["a.py"]


def test_a_team_job_is_recorded_with_its_outcome_and_a_refused_one_is_not():
    done = record_from_events("x", events(call("start_team_job", {"task": "Survey the project"},
                                               "Team job finished (outcome: partial).\nSteps:")))
    assert done.team == ["Survey the project (partial)"]
    refused = record_from_events("x", events(call("start_team_job", {"task": "t"}, "The user declined: no")))
    assert refused.team == []


def test_events_that_do_not_pair_up_are_ignored_without_raising():
    record = record_from_events("x", [None, {"type": "tool_result", "name": "write_file", "result": "ok"},
                                      {"type": "tool_call", "name": "write_file", "args": "not a dict"},
                                      {"type": "final", "content": None}])
    assert record.changed == [] and record.answer == ""


def test_the_entry_has_the_documented_shape():
    record = TurnRecord("Add a docstring to each module", "I added docstrings to 6 modules and the tests pass.",
                        changed=["src/a.py", "src/b.py"], ran=["pytest -q"])
    assert entry_text(record, WHEN) == (
        "## 2026-10-12 14:03 - Add a docstring to each module\n"
        "Changed: src/a.py, src/b.py\n"
        "Ran: pytest -q\n"
        "Answer: I added docstrings to 6 modules and the tests pass.")


def test_empty_fields_are_left_out_and_an_empty_request_has_a_title():
    assert entry_text(TurnRecord(""), WHEN) == "## 2026-10-12 14:03 - (no message)"


def test_more_than_ten_paths_are_counted_not_listed():
    record = TurnRecord("x", changed=[f"f{i}.py" for i in range(13)])
    line = entry_text(record, WHEN).splitlines()[1]
    assert "f9.py" in line and "f10.py" not in line and line.endswith("and 3 more")


def test_a_request_with_a_newline_cannot_start_a_new_entry():
    text = entry_text(TurnRecord("first\n## 2099-01-01 00:00 - fake\nsecond", "ok"), WHEN)
    assert text.count("\n## ") == 0 and text.startswith("## 2026-10-12 14:03 - first")


def test_secrets_are_hidden_everywhere_in_the_entry():
    hide = lambda text: text.replace("sk-secret-123456", "[hidden]")
    record = TurnRecord("use key sk-secret-123456", "key sk-secret-123456 set", changed=["sk-secret-123456.txt"],
                        ran=["curl -H sk-secret-123456 x"], team=["sk-secret-123456 (done)"])
    assert "sk-secret" not in entry_text(record, WHEN, hide)


def test_every_field_is_capped_and_the_whole_entry_stays_under_the_limit():
    record = TurnRecord("r" * 500, "a" * 5000, changed=["p" * 300] * 3 + [f"q{i}" for i in range(10)],
                        ran=["c" * 500] * 12, team=["t" * 500] * 3)
    text = entry_text(record, WHEN)
    assert len(text) <= MAX_ENTRY_CHARS
    assert text.splitlines()[0].count("r") <= 80


def test_a_summary_entry_cannot_forge_headings_and_is_capped():
    text = summary_entry_text("Goal: x\n## 2099 fake heading\n" + "y" * 5000, WHEN)
    assert text.startswith("## 2026-10-12 14:03 - Earlier conversation summarised\n")
    assert "\n## " not in text[3:] and len(text) < 1700


def test_append_creates_the_folder_and_the_file_and_keeps_entries_apart(tmp_path):
    assert append_entry(tmp_path, "## one\nAnswer: a") is True
    assert append_entry(tmp_path, "## two\nAnswer: b") is True
    text = log_path(tmp_path).read_text(encoding="utf-8")
    assert text == "## one\nAnswer: a\n\n## two\nAnswer: b\n\n"


@pytest.mark.skipif(os.name == "nt", reason="file modes are not meaningful on Windows")
def test_the_log_is_readable_by_its_owner_only(tmp_path):
    append_entry(tmp_path, "## one")
    assert (log_path(tmp_path).stat().st_mode & 0o777) == 0o600


def test_a_write_failure_returns_false_instead_of_raising(tmp_path):
    (tmp_path / ".agentx").write_text("a file, not a folder", encoding="utf-8")
    assert append_entry(tmp_path, "## one") is False


def test_rotation_moves_the_oldest_half_to_the_older_file_and_loses_nothing(tmp_path):
    entries = [f"## entry {i:03d}\nAnswer: " + "x" * 80 for i in range(40)]
    for entry in entries:
        assert append_entry(tmp_path, entry, limit=1500) is True
    newer = log_path(tmp_path).read_text(encoding="utf-8")
    older = (tmp_path / ".agentx" / "log-older.md").read_text(encoding="utf-8")
    assert "entry 039" in newer and "entry 000" in older and "entry 000" not in newer
    both = older + newer
    assert all(f"entry {i:03d}" in both for i in range(40))
    assert len(newer.encode("utf-8")) < 3000


def test_read_recent_of_a_missing_log_is_empty(tmp_path):
    assert read_recent(tmp_path) == ""


def test_read_recent_returns_the_newest_entries_in_order_within_the_cap(tmp_path):
    for i in range(30):
        append_entry(tmp_path, f"## entry {i:02d}\nAnswer: " + "x" * 400)
    text = read_recent(tmp_path, max_chars=2000)
    assert len(text) <= 2000 and text.startswith("## entry") and "entry 29" in text
    assert text.index("entry 28") < text.index("entry 29") and "entry 00" not in text


def test_read_recent_of_a_big_log_starts_on_an_entry_boundary(tmp_path):
    for i in range(200):
        append_entry(tmp_path, f"## entry {i:03d}\nAnswer: " + "y" * 300, limit=10_000_000)
    text = read_recent(tmp_path, max_chars=100_000)
    assert text.startswith("## entry ") and "entry 199" in text and len(text) < 25_000


def test_read_recent_skips_a_binary_log(tmp_path):
    log_path(tmp_path).parent.mkdir(parents=True)
    log_path(tmp_path).write_bytes(b"## entry\x00\x01\x02 binary")
    assert read_recent(tmp_path) == ""
