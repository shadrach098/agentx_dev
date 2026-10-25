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
    """A skill read shows its name, never its text; a run of them is one line, printed when the agent moves on."""

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
        lines = console.file.getvalue().splitlines()
        assert lines[0].endswith("Skills  using-superpowers → brainstorming → yt-script (+ score.py)")
        assert lines[1] == "Loading." and "read_path" in lines[2]
        assert "Look before you change" not in console.file.getvalue() and screen.skills_line() == ""

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
