"""A tool's result is one line under its call (like ``⎿ Read 51 lines``); a failure shows its text in red. The
summaries are checked against what the real tools return."""

import io
import sys

import pytest
from rich.console import Console

from agentx_dev.cli.approval import Gate
from agentx_dev.cli.tools import WEB_CAUTION, build_tools
from agentx_dev.cli.ui import Screen, summarize_result


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("x = 4\n", encoding="utf-8")
    return {tool.name: tool for tool in build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path))}


def summary(tools, name, **args):
    return summarize_result(name, tools[name].func(**args))


class TestSummariesOfRealResults:
    def test_reads(self, tools):
        assert summary(tools, "read_path", path="a.py") == ("Read 3 lines", False)
        assert summary(tools, "read_path", path="a.py", offset=2, limit=2) == ("Read lines 2-3", False)

    def test_a_long_file_read_in_parts_says_which_part(self):
        text = "\n".join(f"{n}\tcode" for n in range(1, 6)) + \
            "\n[Showing lines 1-5 of 900; the output is cut at 30,000 characters. Continue with ...]"
        assert summarize_result("read_path", text) == ("Read lines 1-5 of 900", False)

    def test_listing_and_searching(self, tools):
        assert summary(tools, "list_directory", path=".") == ("Listed 2 entries", False)
        assert summary(tools, "find_files", glob="**/*.py") == ("Found 2 files", False)
        assert summary(tools, "grep", pattern="x") == ("2 matches in 2 files searched", False)
        assert summary(tools, "grep", pattern="nothing-here") == ("No matches", False)

    def test_edits_keep_their_own_line(self, tools):
        text, failed = summary(tools, "write_file", path="c.txt", content="hi", if_exists="refuse")
        assert text.startswith("created: 2 bytes") and not failed
        assert summary(tools, "edit_file", path="c.txt", find="hi", replace="yo") == \
            ("edited c.txt: replaced 1 occurrence", False)

    def test_commands_show_their_first_line_or_how_they_failed(self, tools):
        assert summary(tools, "run_python", code="print('one'); print('two')") == ("one  (+1 more line)", False)
        assert summary(tools, "run_python", code="pass")[0] != ""
        text, failed = summary(tools, "run_python", code="1/0")
        assert failed and text.startswith("Exit code 1") and "ZeroDivisionError" in text

    def test_shell_output_without_its_header(self, tools):
        text, failed = summary(tools, "run_shell", command=f'"{sys.executable}" -c "print(42)"')
        if "bash" not in str(tools["run_shell"].func(command="echo x")).lower() and text.startswith("$"):
            pytest.skip("no shell here")
        assert text == "42" and not failed

    def test_recall_web_and_mcp(self):
        notes = ("These notes were written by agentx from earlier sessions in this folder. ...\n\n"
                 "- 2026-10-25 16:13 lets proceed (.agentx/notes/a.md):\n  summary\n- 2026-10-24 x (b.md):\n")
        assert summarize_result("recall", notes) == ("Found 2 notes", False)
        assert summarize_result("web_search", WEB_CAUTION + "(via DuckDuckGo)\n1. A\n   u\n2. B\n   u") == \
            ("Found 2 results", False)
        assert summarize_result("web_fetch", WEB_CAUTION + "x" * 1234) == ("Fetched 1,234 characters", False)
        mcp = "[MCP result from the server 'f': data ...]\nC$2,111 total\nAT211\nAT517"
        assert summarize_result("flightclaw__search_flights", mcp) == ("C$2,111 total  (+2 more lines)", False)

    def test_nothing_is_no_output(self):
        assert summarize_result("anything", "") == ("No output", False)


class TestOnScreen:
    def screen(self):
        console = Console(file=io.StringIO(), width=120, color_system=None)
        return Screen(console), console

    def test_one_line_under_the_call(self, tools):
        screen, console = self.screen()
        screen.show_event({"type": "tool_result", "name": "read_path", "is_error": False,
                           "result": tools["read_path"].func(path="a.py")})
        assert console.file.getvalue() == "  ⎿ Read 3 lines\n"

    def test_a_refusal_or_error_is_shown_in_full(self):
        screen, console = self.screen()
        screen.show_event({"type": "tool_result", "name": "write_file", "is_error": False,
                           "result": "Refused: the settings deny rule 'write_file:*' blocks this."})
        screen.show_event({"type": "tool_result", "name": "read_path", "is_error": True,
                           "result": "ERROR: path does not exist: x\nsecond line"})
        text = console.file.getvalue()
        assert "Refused: the settings deny rule" in text and "ERROR: path does not exist: x" in text
        assert "second line" in text and "⎿" not in text

    def test_a_console_without_the_marker_gets_a_plain_one(self):
        raw = io.BytesIO()
        console = Console(file=io.TextIOWrapper(raw, encoding="cp1252", newline=""), width=120, color_system=None)
        Screen(console).show_event({"type": "tool_result", "name": "grep", "is_error": False,
                                    "result": "(no matches ...)"})
        console.file.flush()
        assert raw.getvalue().decode("cp1252") == "  -> No matches\n"
