"""Rendering, the approval prompt, and the completer. No real terminal needed."""

import io

import pytest
from rich.console import Console

from agentx_dev.cli.approval import Request
from agentx_dev.cli.ui import Screen, bullet, make_asker, make_completer, plain


def console(**kw):
    out = io.StringIO()
    return Console(file=out, width=100, force_terminal=False, color_system=None, **kw), out


def styled_console():
    """A console that would emit real ANSI codes if anything asked it to."""
    out = io.StringIO()
    return Console(file=out, width=100, force_terminal=True, color_system="standard"), out


class TestScreen:
    def test_a_tool_call_is_one_line_with_a_summary(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_call", "name": "edit_file", "args": {"path": "src/app.py"}})
        assert out.getvalue().strip().endswith("edit_file  src/app.py") and "●" in out.getvalue()

    def test_a_result_is_collapsed_after_three_lines(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_result", "name": "read_path", "is_error": False,
                              "result": "\n".join(f"line{i}" for i in range(10))})
        text = out.getvalue()
        assert "line0" in text and "line2" in text and "line3" not in text and "... 7 more lines" in text

    def test_square_brackets_in_output_are_not_markup(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_result", "name": "run_shell", "is_error": False,
                              "result": "[bold]not markup[/bold] and [red]x"})
        assert "[bold]not markup[/bold]" in out.getvalue()

    def test_thoughts_finals_notices_and_errors(self):
        c, out = console()
        s = Screen(c)
        s.show_event({"type": "thought", "content": "thinking about it"})
        s.show_event({"type": "final", "content": "**Done** with the task"})
        s.show_event({"type": "text_delta", "content": "ignored"})
        s.show_event({"type": "completion", "completion": object()})
        s.notice("hello")
        s.error("boom")
        text = out.getvalue()
        assert "thinking about it" in text and "Done with the task" in text
        assert "ignored" not in text and "hello" in text and "boom" in text

    def test_a_console_that_cannot_print_the_bullet_gets_a_star(self):
        legacy = Console(file=io.TextIOWrapper(io.BytesIO(), encoding="cp1252"), force_terminal=False)
        assert bullet(legacy) == "*"
        c, _ = console()
        assert bullet(c) == "●"

    def test_a_tool_call_with_non_dict_args_does_not_crash(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_call", "name": "mystery", "args": "plain text"})
        assert "mystery" in out.getvalue()


class TestNothingModelSuppliedCanStyleOrSpoofTheScreen:
    ESC = "\x1b[2J\x1b[31mFAKE\x1b[0m"

    def test_plain_defuses_escapes_and_direction_overrides(self):
        text = plain("a\x1b[31mb\x07\rc‮d\x9b")
        assert "\x1b" not in text and "\x07" not in text and "\r" not in text
        assert "‮" not in text and "\x9b" not in text
        assert plain("tab\tand\nnewline", keep_newlines=True) == "tab\tand\nnewline"
        assert plain("héllo ✓ 日本 👨‍👩") == "héllo ✓ 日本 👨‍👩"

    def test_results_thoughts_calls_notices_and_errors_carry_no_escape_sequences(self):
        c, out = styled_console()
        s = Screen(c)
        s.show_event({"type": "tool_result", "name": "x", "is_error": False, "result": self.ESC})
        s.show_event({"type": "thought", "content": self.ESC})
        s.show_event({"type": "tool_call", "name": "x\x1b[31m", "args": {"path": self.ESC}})
        s.notice(self.ESC)
        s.error(self.ESC)
        text = out.getvalue()
        assert "\x1b[2J" not in text            # the model's own clear-screen never reaches the terminal
        assert "\x1b[31mFAKE" not in text       # nor its red "FAKE" (rich's own styling is never followed by FAKE)

    def test_a_final_answer_is_markdown_but_cannot_inject_escapes_or_links(self):
        c, out = styled_console()
        Screen(c).show_event({"type": "final", "content": f"**bold** {self.ESC} [click](http://evil.example)"})
        text = out.getvalue()
        assert "\x1b[2J" not in text and "\x1b]8;" not in text   # no OSC 8 hyperlink either
        assert "FAKE" in text and "bold" in text

    def test_a_tool_call_name_cannot_break_the_line(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_call", "name": "evil\nFAKE LINE", "args": {}})
        assert len(out.getvalue().strip().splitlines()) == 1


def lines(*items):
    queue = list(items)

    def read(prompt=""):
        read.prompts.append(prompt)
        if not queue:
            raise EOFError
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    read.prompts = []
    return read


def edit_request():
    return Request(tool="write_file", kind="edit", subject="a.txt",
                   args={"path": "a.txt", "content": "hi\n", "if_exists": "refuse"},
                   remember=True, grant="all edits this session")


class TestApprovalPrompt:
    def test_yes_shows_the_diff_and_returns_y(self, tmp_path):
        c, out = console()
        answer = make_asker(c, lines("y"), tmp_path)(edit_request())
        assert answer.choice == "y"
        text = out.getvalue()
        assert "Create a.txt" in text and "+hi" in text and "[a] yes, don't ask again" in text

    def test_the_prompt_says_exactly_what_always_will_remember(self, tmp_path):
        c, out = console()
        make_asker(c, lines("y"), tmp_path)(edit_request())
        assert "all edits this session" in out.getvalue()

    def test_a_returns_a(self, tmp_path):
        c, _ = console()
        assert make_asker(c, lines("a"), tmp_path)(edit_request()).choice == "a"

    def test_no_asks_for_a_reason(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines("n", "wrong file"), tmp_path)(edit_request())
        assert (answer.choice, answer.reason) == ("n", "wrong file")

    def test_garbage_is_asked_again(self, tmp_path):
        c, out = console()
        answer = make_asker(c, lines("maybe", "perhaps", "yes"), tmp_path)(edit_request())
        assert answer.choice == "y" and out.getvalue().count("Please answer") == 2

    def test_bare_enter_is_a_no(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines("", ""), tmp_path)(edit_request())
        assert answer.choice == "n"

    def test_ctrl_d_declines(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines(), tmp_path)(edit_request())
        assert answer.choice == "n" and answer.reason

    def test_ctrl_c_declines_and_does_not_run_the_tool(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines(KeyboardInterrupt()), tmp_path)(edit_request())
        assert answer.choice == "n" and answer.reason

    def test_ctrl_c_at_the_reason_prompt_is_still_a_no(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines("n", KeyboardInterrupt()), tmp_path)(edit_request())
        assert (answer.choice, answer.reason) == ("n", "")

    def test_actions_the_gate_cannot_remember_do_not_offer_always(self, tmp_path):
        c, out = console()
        request = Request(tool="delete_path", kind="destructive", subject="x", args={"path": "x"},
                          remember=False)
        answer = make_asker(c, lines("a", "always", "y"), tmp_path)(request)
        assert answer.choice == "y"
        text = out.getvalue()
        assert "[a]" not in text and "don't ask again" not in text and "Please answer y or n." in text

    def test_a_command_is_shown_as_typed(self, tmp_path):
        c, out = console()
        request = Request(tool="run_shell", kind="command", subject="pytest -q", args={"command": "pytest -q [x]"},
                          remember=True, grant="pytest")
        make_asker(c, lines("n", ""), tmp_path)(request)
        text = out.getvalue()
        assert "Run shell command" in text and "pytest -q [x]" in text and "pytest" in text

    def test_markup_and_escapes_in_the_preview_and_title_are_inert(self, tmp_path):
        c, out = styled_console()
        request = Request(tool="run_shell", kind="command", subject="x",
                          args={"command": "echo [bold red]ok[/] \x1b[2J ‮", "cwd": "[green]dir\x1b[0m"},
                          remember=True, grant="echo [red]x[/red]")
        make_asker(c, lines("n", ""), tmp_path)(request)
        text = out.getvalue()
        assert "\x1b[2J" not in text and "‮" not in text
        assert "[bold red]ok[/]" in text and "[green]dir" in text and "[red]x[/red]" in text

    def test_a_file_name_cannot_break_the_title_onto_a_line_of_its_own(self, tmp_path):
        c, out = console()
        request = Request(tool="delete_path", kind="destructive", subject="x",
                          args={"path": "x\n[a] yes, don't ask again"}, remember=False)
        make_asker(c, lines("y"), tmp_path)(request)
        first = out.getvalue().splitlines()[0]
        assert "Delete x" in first and "[a] yes, don't ask again" in first   # all on the one border line


class TestCompleter:
    def complete(self, completer, text):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    def test_slash_commands_complete(self):
        completer = make_completer(["/help", "/model", "/mode"])
        assert self.complete(completer, "/mo") == ["/model", "/mode"]
        assert self.complete(completer, "/") == ["/help", "/model", "/mode"]
        assert self.complete(completer, "/zzz") == []

    def test_paths_complete_after_a_word(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        completer = make_completer(["/help"])
        found = self.complete(completer, f"open {tmp_path.as_posix()}/no")
        assert "tes.txt" in found      # the rest of "notes.txt" after the typed "no"
