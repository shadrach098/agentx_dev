"""A team helper's looking calls are one line per helper (``● [explorer] Read 12 files · Searched 3 patterns``),
the way the main chat's skill reads are; edits and commands keep a line each, with project-relative paths."""

import io

from rich.console import Console

from agentx_dev.cli.team import StepStatus, step_label
from agentx_dev.cli.ui import Progress, Screen, helper_activity, short_time, short_tokens


def screen(encoding="utf-8"):
    out = io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")
    console = Console(file=out, width=200, color_system=None)
    return Screen(console), out


def text(out):
    out.flush()
    return out.buffer.getvalue().decode(out.encoding)


def test_looking_calls_are_counted_not_printed():
    s, out = screen()
    s.helper_call("explorer", "read_path", {"path": "a.py"})
    s.helper_call("explorer", "read_path", {"path": "b.py"})
    s.helper_call("explorer", "read_path", {"path": "a.py"})      # the same file: still two files
    s.helper_call("explorer", "grep", {"pattern": "x"})
    s.helper_call("explorer", "find_files", {"glob": "*.py"})
    s.helper_call("explorer", "web_search", {"query": "q"})
    assert text(out) == ""
    assert s.helpers_line() == "explorer: Read 2 files · Searched 2 patterns · Searched the web once"


def test_the_line_is_printed_when_the_step_ends():
    s, out = screen()
    s.helper_call("explorer", "read_path", {"path": "a.py"})
    s.helper_call("explorer", "list_directory", {"path": "."})
    s.team_result("explorer", True, "found it")
    lines = text(out).splitlines()
    assert lines == ["● [explorer] Read 1 file · Listed 1 folder", "   done (explorer): found it"]
    assert s.helpers_line() == ""


def test_an_edit_prints_the_line_so_far_then_its_own():
    s, out = screen()
    s.cwd = "C:\\work\\proj"
    s.helper_call("coder", "read_path", {"path": "a.py"})
    s.helper_call("coder", "edit_file", {"path": "C:\\work\\proj\\src\\a.py", "find": "x", "replace": "y"})
    lines = text(out).splitlines()
    assert lines[0] == "● [coder] Read 1 file"
    assert lines[1].startswith("● [coder] edit_file") and lines[1].endswith(" src/a.py") and "work" not in lines[1]


def test_two_helpers_side_by_side_keep_their_own_counts():
    s, out = screen()
    s.helper_call("explorer", "read_path", {"path": "a.py"})
    s.helper_call("explorer@gpt-5.5", "web_fetch", {"url": "https://x"})
    s.helper_call("explorer@gpt-5.5", "web_fetch", {"url": "https://y"})
    assert s.helpers_line() == "explorer: Read 1 file; explorer@gpt-5.5: Fetched 2 pages"
    s.flush_helpers()
    assert text(out).splitlines() == ["● [explorer] Read 1 file", "● [explorer@gpt-5.5] Fetched 2 pages"]


def test_skills_are_named():
    assert helper_activity({"skill": {"exploring-a-project": None, "web-research": None}}) == \
        "Skills exploring-a-project, web-research"


def test_a_console_without_the_dot_gets_plain_separators():
    s, out = screen("cp1252")
    s.helper_call("explorer", "read_path", {"path": "a.py"})
    s.helper_call("explorer", "grep", {"pattern": "x"})
    s.flush_helpers()
    assert text(out) == "* [explorer] Read 1 file, Searched 1 pattern\n"


def test_markup_in_a_helper_name_stays_text():
    s, out = screen()
    s.helper_call("[red]x[/red]", "read_path", {"path": "a.py"})
    s.flush_helpers()
    assert "[red]x[/red]" in text(out)


def test_odd_arguments_never_raise():
    s, _ = screen()
    s.helper_call(None, "read_path", None)
    s.helper_call("explorer", "use_skill", "plain text")
    s.helper_call("explorer", "read_path", {"Input": {"path": "a.py"}})
    s.flush_helpers()


# -- the live block of running team steps, and each step's line when it ends -----------------------------------

def running(label="security audit", helper="explorer@gpt-6.1-sol"):
    status = StepStatus(1, helper, label, "q")
    status.note_call("read_path", {"path": "a.py"}, "1\tx = 1")
    status.note_call("read_path", {"path": "b.py"}, "1\ty = 2")
    status.note_call("grep", {"pattern": "x"}, "(no matches ...)")
    status.note_call("run_python", {"code": "print(1)"}, "1")
    status.note_skill("exploring-a-project")
    status.tokens = 38_400
    return status


def test_the_block_has_one_line_per_running_step():
    s, out = screen()
    first, second = running(), running("payments", "explorer")
    s.console.print(s.team_board([first, second], done=1, total=5))
    lines = text(out).splitlines()
    assert lines[0] == "● Team · 2 running · 1 of 5 done"
    assert lines[1].startswith("  ├ explorer@gpt-6.1-sol · security audit  Read 2 files · Searched 1 pattern · "
                               "Ran 1 command · Skills exploring-a-project  0s · 38k tokens")
    assert lines[2].startswith("  └ explorer · payments")
    assert s.team_board([]) is None


def test_the_spinner_draws_the_block_above_it():
    console = Console(file=io.StringIO(), width=200, force_terminal=True, color_system=None)
    progress = Progress(console)
    s = Screen(console)
    progress.board = lambda: s.team_board([running()])
    progress.start("Team is working")
    try:
        console.print(progress.__rich__())
    finally:
        progress.stop()
    drawn = console.file.getvalue()
    assert "● Team · 1 running" in drawn and "security audit" in drawn and "Team is working..." in drawn


def test_a_finished_step_is_one_line_and_the_start_of_its_report():
    s, out = screen()
    status = running()
    status.ended = status.started + 90
    s.team_done("explorer", "security audit", True, "3 high findings\nmore", status=status)
    s.team_done("coder", "fix it", False, "boom")
    s.team_done("runner", "check", False, "not started: the user chose to wrap up at the token limit", skipped=True)
    lines = text(out).splitlines()
    assert lines[0] == "✓ explorer · security audit  Done (5 tool uses · 38k tokens · 1m 30s)"
    assert lines[1] == "  ⎿ 3 high findings more"
    assert lines[2] == "✗ coder · fix it  Failed" and lines[3] == "  ⎿ boom"
    assert lines[4] == "– runner · check  Skipped"


def test_a_plain_console_gets_plain_marks():
    s, out = screen("cp1252")
    s.team_done("explorer", "look", True, "ok")
    s.console.print(s.team_board([running()]))
    shown = text(out)
    assert shown.startswith("+ explorer") and "✓" not in shown and "└" not in shown
    assert "`- explorer@gpt-6.1-sol" in shown


def test_the_log_lists_every_call_with_its_result():
    s, out = screen()
    s.team_log([running()])
    shown = text(out)
    assert shown.splitlines()[0].startswith("● explorer@gpt-6.1-sol · security audit  (5 tool uses · 38k tokens")
    assert "read_path  a.py" in shown and "⎿ Read lines 1-1" in shown and "use_skill" in shown


def test_short_numbers():
    assert [short_tokens(n) for n in (0, 999, 4_200, 38_400, 2_500_000)] == ["0", "999", "4.2k", "38k", "2.5m"]
    assert [short_time(n) for n in (0, 59, 90, 3600, 89.99999)] == ["0s", "59s", "1m 30s", "60m 00s", "1m 30s"]


def test_a_step_is_named_by_its_id_or_its_task():
    assert step_label({"id": "security_audit", "query": "x"}) == "security audit"
    assert step_label({"id": "s1", "query": "Look at the payment webhooks and refunds"}) == "Look at the payment webhooks"
    assert step_label({"id": "step_3", "query": "check"}) == "check"
