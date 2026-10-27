"""The start screen and the input line: the welcome box, warnings, barred notes, the rule above the input and the
mode line under it."""

import io

from rich.console import Console

from agentx_dev.cli.app import WHATS_NEW
from agentx_dev.cli.ui import Callout, LOGO, MODE_LINES, Screen, prompt_message, status_line, welcome_panel
from tests.conftest import MockModel
from tests.test_cli_app import Lines, make_app


def render(renderable, width=120):
    console = Console(file=io.StringIO(), width=width, color_system=None, force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()


def panel(width=120, **kw):
    parts = dict(version="3.8.0", greeting="Welcome back bruce!", model="gpt-6.1-sol · OpenAI · auto mode",
                 folder="~/work/app", tips=["Type / to see the commands"], news=["MCP servers"])
    parts.update(kw)
    return welcome_panel(width=width, **parts)


class TestWelcomeBox:
    def test_it_holds_the_version_greeting_mark_model_folder_tips_and_news(self):
        text = render(panel())
        for part in ("agentx v3.8.0", "Welcome back bruce!", LOGO[0].strip(), "gpt-6.1-sol", "~/work/app",
                     "Tips for getting started", "Type / to see the commands", "What's new", "MCP servers",
                     "/help lists the commands"):
            assert part in text
        assert "│" in text                                    # the line between the two sides

    def test_a_narrow_screen_stacks_the_two_sides(self):
        text = render(panel(width=70), width=70)
        lines = text.splitlines()
        assert lines.index(next(l for l in lines if "Welcome back" in l)) < \
            lines.index(next(l for l in lines if "Tips for getting started" in l))
        assert all(len(line) <= 70 for line in lines)

    def test_nothing_in_it_is_read_as_markup_or_escape_codes(self):
        text = render(panel(folder="[bold]x[/bold]\x1b[31m", greeting="hi [red]"))
        assert "[bold]x[/bold]" in text and "\x1b" not in text and "hi [red]" in text


class TestNotesAndWarnings:
    def test_every_wrapped_line_of_a_callout_has_the_bar(self):
        text = render(Callout("Auto mode is on.", "word " * 60), width=50)
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) > 3 and all(line.startswith("▎ ") for line in lines)
        assert lines[0] == "▎ Auto mode is on."

    def test_a_warning_has_its_hint(self):
        console = Console(file=io.StringIO(), width=120, color_system=None)
        Screen(console).warning("1 MCP server needs sign-in: github", "run /mcp login github")
        assert console.file.getvalue().strip() == "⚠ 1 MCP server needs sign-in: github · run /mcp login github"


class TestTheInputLine:
    def test_a_rule_then_the_mark(self):
        parts = prompt_message(40)
        assert parts[0] == ("class:rule", "─" * 39 + "\n") and parts[1] == ("class:prompt", "❯ ")

    def test_the_mode_line_for_each_mode(self):
        for mode, (marker, words, _) in MODE_LINES.items():
            text = "".join(t for _, t in status_line(mode, "gpt-6.1-sol", "app", 60))
            assert f"{marker}{words}" in text and "(shift+tab to cycle)" in text and "gpt-6.1-sol · app" in text
        assert "auto mode on" in "".join(t for _, t in status_line("auto", "m", "f", 60))

    def test_unknown_mode_and_odd_text_are_safe(self):
        text = "".join(t for _, t in status_line("weird", "m\x1b[31m", "f", 60))
        assert "default mode" in text and "\x1b" not in text


class TestStartScreen:
    def test_start_shows_the_box_and_the_auto_note(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), width=140, mode="auto")
        assert app.start() == 0
        text = out.getvalue()
        assert "agentx v" in text and "Tips for getting started" in text and WHATS_NEW[0] in text
        assert "▎ Auto mode is on." in text
        assert "Run /init to create an AGENTX.md" in text           # the project has none

    def test_a_project_with_agentx_md_gets_no_init_tip(self, tmp_path):
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "AGENTX.md").write_text("notes", encoding="utf-8")
        app, out, cwd, home = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), width=140)
        app.start()
        assert "Run /init" not in out.getvalue()

    def test_the_status_bar_follows_the_mode(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines("/exit"), MockModel(script=[]))
        app.start()
        assert "default mode" in "".join(t for _, t in app.status_bar())
        app.cycle_mode()
        assert "accept edits on" in "".join(t for _, t in app.status_bar())

    def test_a_long_folder_is_shortened_from_the_front(self, tmp_path):
        deep = tmp_path / ("x" * 30) / ("y" * 30)
        deep.mkdir(parents=True)
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(script=[]))
        app.cwd = deep
        short = app._short_folder()
        assert len(short) == 42 and short.startswith("…") and short.endswith("y" * 30)


class TestSkillReadsAreOneLine:
    """A skill read shows its name, never its text; a run of them is one step of the look-around flow, printed when
    the agent moves on to something else."""

    def screen(self):
        console = Console(file=io.StringIO(), width=200, color_system=None)
        return Screen(console), console

    def call(self, name, file=None):
        args = {"name": name, **({"file": file} if file else {})}
        return {"type": "tool_call", "name": "use_skill", "args": args}

    def result(self, text="# A skill\nLook before you change anything", error=False):
        return {"type": "tool_result", "name": "use_skill", "result": text, "is_error": error}

    def test_a_run_of_reads_becomes_one_line_and_notes_wait_for_it(self):
        screen, console = self.screen()
        for event in (self.call("using-superpowers"), self.result(), {"type": "thought", "content": "Loading."},
                      self.call("brainstorming"), self.result(), self.call("yt-script"), self.result(),
                      self.call("yt-script", "score.py"), self.result("#!/usr/bin/env python3")):
            screen.show_event(event)
        assert console.file.getvalue() == ""                     # nothing yet: the run is still open
        assert screen.skills_line() == "using-superpowers → brainstorming → yt-script (+ score.py)"
        screen.show_event({"type": "tool_call", "name": "read_path", "args": {"path": "a.py"}})
        assert console.file.getvalue() == ""                     # a read joins the flow
        screen.show_event({"type": "thought", "content": "Now run it."})
        screen.show_event({"type": "tool_call", "name": "run_shell", "args": {"command": "pytest"}})
        lines = console.file.getvalue().splitlines()
        assert lines[0].endswith("Skills  using-superpowers → brainstorming → yt-script (+ score.py)")
        assert lines[1] == "└─ read_path  a.py"
        assert lines[2] == "Now run it." and "run_shell" in lines[3]       # a note before a command is kept
        text = console.file.getvalue()
        assert "Loading." not in text                                      # a note leading into a look is not
        assert "Look before you change" not in text and screen.skills_line() == ""

    def test_the_final_answer_closes_the_run(self):
        screen, console = self.screen()
        screen.show_event(self.call("brainstorming"))
        screen.show_event(self.result())
        screen.show_event({"type": "final", "content": "Done."})
        text = console.file.getvalue()
        assert text.index("Skills  brainstorming") < text.index("Done.")

    def test_a_failed_read_is_shown(self):
        screen, console = self.screen()
        screen.show_event(self.call("flights"))
        screen.show_event(self.result("ERROR: no skill named 'flights'.", error=True))
        screen.flush()
        text = console.file.getvalue()
        assert "Skills  flights" in text and "ERROR: no skill named 'flights'." in text

    def test_reading_a_skill_again_does_not_repeat_it(self):
        screen, console = self.screen()
        for _ in range(2):
            screen.show_event(self.call("brainstorming"))
            screen.show_event(self.result())
        assert screen.skills_line() == "brainstorming"

    def test_flush_twice_prints_once(self):
        screen, console = self.screen()
        screen.show_event(self.call("brainstorming"))
        screen.flush()
        screen.flush()
        assert console.file.getvalue().count("Skills") == 1

    def test_in_a_real_turn_the_skill_text_never_reaches_the_screen(self, tmp_path):
        from tests.conftest import make_final, make_react_response
        model = MockModel(script=[make_react_response("use_skill", {"name": "exploring-a-project"}),
                                  make_react_response("use_skill", {"name": "changing-files"}),
                                  make_final("All read.")])
        app, out, cwd, home = make_app(tmp_path, Lines("read the skills", "/exit"), model, width=200)
        assert app.start() == 0
        text = out.getvalue()
        assert "Skills  exploring-a-project → changing-files" in text and text.count("Skills  ") == 1
        assert "Look before you change anything" not in text and "All read." in text


class TestTheLookAroundFlow:
    """Skill reads, recall, reads, searches and web calls in a row are one tree, drawn in place while it grows and
    printed when the agent moves on; the agent's narration leading into them, or into its answer, is not printed."""

    def screen(self, encoding="utf-8"):
        out = io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")
        console = Console(file=out, width=200, color_system=None)
        return Screen(console), out

    @staticmethod
    def text(out):
        out.flush()
        return out.buffer.getvalue().decode(out.encoding)

    def where_were_we(self, screen):
        notes = ("These notes were written by agentx from earlier sessions in this folder. ...\n\n"
                 "- 2026-10-25 a (.agentx/notes/a.md):\n  x\n- 2026-10-24 b (b.md):\n")
        for event in (
                {"type": "thought", "content": "The user wants a recap. First read the conversation-start skill."},
                {"type": "tool_call", "name": "use_skill", "args": {"name": "using-superpowers"}},
                {"type": "tool_result", "name": "use_skill", "result": "# Using superpowers\nlong text"},
                {"type": "thought", "content": "Using the conversation-start skill; retrieve the prior notes."},
                {"type": "tool_call", "name": "recall", "args": {"query": "most recent work"}},
                {"type": "tool_result", "name": "recall", "result": notes},
                {"type": "thought", "content": "Read the audit note for the exact three issues."},
                {"type": "tool_call", "name": "read_path", "args": {"path": ".agentx/notes/a.md"}},
                {"type": "tool_result", "name": "read_path", "result": "1\tx\n2\ty\n3\tz"},
                {"type": "thought", "content": "Frame these as historical results."}):
            screen.show_event(event)

    def test_the_users_example(self):
        screen, out = self.screen()
        self.where_were_we(screen)
        assert self.text(out) == ""                                       # still open: drawn in place
        screen.show_event({"type": "final", "content": "We had finished the repo audit."})
        lines = self.text(out).splitlines()
        assert lines[:5] == ["● Skills  using-superpowers",
                             "├─ recall  most recent work",
                             "│  ⎿ Found 2 notes",
                             "└─ read_path  .agentx/notes/a.md",
                             "   ⎿ Read lines 1-3"]
        assert "We had finished the repo audit." in lines[5]
        shown = self.text(out)
        assert "long text" not in shown and "conversation-start" not in shown and "historical" not in shown

    def test_while_it_runs_the_flow_is_drawn_and_the_running_step_is_yellow(self):
        screen, out = self.screen()
        screen.show_event({"type": "tool_call", "name": "use_skill", "args": {"name": "using-superpowers"}})
        screen.show_event({"type": "tool_result", "name": "use_skill", "result": "text"})
        screen.show_event({"type": "tool_call", "name": "grep", "args": {"pattern": "x", "path": "src"}})
        lines = screen._flow_lines(live=True)
        assert lines[0].style == "bold yellow"                            # the flow is running
        assert any("yellow" in str(span.style) for span in lines[1].spans)       # and so is grep
        assert not any("yellow" in str(span.style) for span in lines[0].spans)   # the skill read is done
        assert screen.flow_block() is not None
        screen.flush()
        assert screen.flow_block() is None and "yellow" not in str([line.spans for line in screen._flow_lines(False)])

    def test_a_failed_step_is_red_in_the_flow_and_a_command_ends_it(self):
        screen, out = self.screen()
        screen.show_event({"type": "tool_call", "name": "read_path", "args": {"path": "nope.py"}})
        screen.show_event({"type": "tool_result", "name": "read_path", "result": "ERROR: path does not exist: nope.py",
                           "is_error": True})
        screen.show_event({"type": "thought", "content": "Run the tests instead."})
        screen.show_event({"type": "tool_call", "name": "run_shell", "args": {"command": "pytest"}})
        lines = self.text(out).splitlines()
        assert lines[0] == "● read_path  nope.py" and lines[1] == "  ⎿ ERROR: path does not exist: nope.py"
        assert lines[2] == "Run the tests instead." and lines[3].startswith("● run_shell")

    def test_a_plain_console_gets_a_plain_tree(self):
        screen, out = self.screen("cp1252")
        self.where_were_we(screen)
        screen.flush()
        lines = self.text(out).splitlines()
        assert lines[0] == "* Skills  using-superpowers" and lines[1].startswith("|- recall")
        assert lines[3].startswith("`- read_path")

    def test_the_app_draws_the_open_flow_above_the_spinner(self, tmp_path):
        from tests.test_cli_app import Lines, make_app
        from tests.conftest import MockModel
        app, out, _, _ = make_app(tmp_path, Lines(), MockModel())
        app.screen.show_event({"type": "tool_call", "name": "grep", "args": {"pattern": "x"}})
        assert app._board() is not None
