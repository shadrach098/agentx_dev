"""A team helper's looking calls are one line per helper (``● [explorer] Read 12 files · Searched 3 patterns``),
the way the main chat's skill reads are; edits and commands keep a line each, with project-relative paths."""

import io

from rich.console import Console

from agentx_dev.cli.ui import Screen, helper_activity


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
