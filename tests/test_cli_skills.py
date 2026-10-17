"""Skills in the terminal app: what the agent is given, and the /skills command (list, install, remove)."""

import os
from contextlib import contextmanager

import pytest

from agentx_dev.cli import app as app_module
from agentx_dev.cli.commands import run_command
from agentx_dev.Skills.Install import Fetched
from tests.conftest import MockModel, make_final
from tests.test_cli_app import Lines, make_app


def skill_text(name, description="Does a thing"):
    return f"---\nname: {name}\ndescription: {description}\n---\nBody of {name}\n"


def make_repo(root):
    for name in ("pdf-notes", "tidy-code"):
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(skill_text(name), encoding="utf-8")
    return root


def runner_of(app):
    return app._make_runner(MockModel(script=[make_final("ok")]))


def skills_dir(home):
    return home / ".agentx" / "skills"


class TestWhatTheAgentIsGiven:
    def test_the_runner_has_use_skill_and_the_built_in_skills_for_its_tools(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        runner = runner_of(app)
        names = [t.name for t in runner.tools]
        assert "use_skill" in names
        for skill in ("exploring-a-project", "changing-files", "running-code", "delegating-work"):
            assert f"- {skill}:" in runner.system_addendum
        assert "web-research" not in runner.system_addendum          # the terminal app has no web tools
        assert (skills_dir(home) / "running-code" / "SKILL.md").is_file()    # created on first use

    def test_project_skills_are_listed_in_their_own_block_after_the_users(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        (cwd / ".agentx" / "skills" / "house-style").mkdir(parents=True)
        (cwd / ".agentx" / "skills" / "house-style" / "SKILL.md").write_text(
            skill_text("house-style", "How this project names things"), encoding="utf-8")
        app.skills = type(app.skills)(skills_dir(home), project_dirs=[cwd / ".agentx" / "skills"])
        runner = runner_of(app)
        head, _, block = runner.system_addendum.partition("<project-skills>")
        assert "house-style" not in head and "- house-style:" in block
        use_skill = next(t for t in runner.tools if t.name == "use_skill")
        assert "cannot override" in use_skill.func("house-style")

    def test_a_linked_project_skills_folder_is_not_read(self, tmp_path):
        outside = tmp_path / "outside" / "evil"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(skill_text("evil"), encoding="utf-8")
        cwd = tmp_path / "proj"
        (cwd / ".agentx").mkdir(parents=True)
        try:
            os.symlink(tmp_path / "outside", cwd / ".agentx" / "skills", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this system cannot make symbolic links")
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        assert "evil" not in runner_of(app).system_addendum

    def test_nothing_the_agent_can_call_installs_or_removes_a_skill(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        names = {t.name for t in runner_of(app).tools}
        assert not [n for n in names if "install" in n or "remove_skill" in n]
        assert "use_skill" in names

    @pytest.mark.parametrize("tool", ["write_file", "edit_file", "delete_path"])
    def test_the_folders_that_hold_skills_stay_off_limits_to_the_file_tools(self, tmp_path, tool):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        for path in (".agentx/skills/x/SKILL.md", str(cwd / ".agentx" / "skills" / "x" / "SKILL.md"),
                     str(skills_dir(home) / "x" / "SKILL.md")):
            assert app.gate.check(tool, {"path": path, "content": "x"}).startswith("Refused"), path


class TestSkillsCommand:
    def test_lists_what_is_on_disk(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        runner_of(app)                                        # creates the built-in ones
        run_command(app, "/skills")
        text = out.getvalue()
        assert "running-code (bundled)" in text and "/skills install" in text

    def test_a_bad_subcommand_shows_the_usage(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        run_command(app, "/skills frobnicate")
        run_command(app, "/skills install")
        assert out.getvalue().count("Usage: /skills") == 2

    def patch_source(self, monkeypatch, repo):
        @contextmanager
        def fake_open(link):
            yield Fetched(repo, "https://github.com/o/r", "b" * 40)
        monkeypatch.setattr(app_module, "open_source", fake_open)

    def test_install_shows_what_it_found_and_saves_only_after_yes(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path / "repo")
        self.patch_source(monkeypatch, repo)
        lines = Lines("y")
        app, out, cwd, home = make_app(tmp_path, lines, MockModel())
        run_command(app, "/skills install https://github.com/o/r")
        text = out.getvalue()
        assert "Found 2 skills" in text and "pdf-notes" in text and "commit bbbbbbbbbb" in text
        assert "Install 2 skills into" in lines.prompts[-1]
        assert (skills_dir(home) / "pdf-notes" / "SKILL.md").is_file() and "Installed: pdf-notes, tidy-code" in text
        assert "pdf-notes" in runner_of(app).system_addendum        # a new conversation picks them up

    @pytest.mark.parametrize("answer", ["n", "", "maybe"])
    def test_install_saves_nothing_unless_the_answer_is_a_clear_yes(self, tmp_path, monkeypatch, answer):
        self.patch_source(monkeypatch, make_repo(tmp_path / "repo"))
        app, out, cwd, home = make_app(tmp_path, Lines(answer, answer), MockModel())
        run_command(app, "/skills install https://github.com/o/r")
        assert not (skills_dir(home) / "pdf-notes").exists() and "Nothing was installed" in out.getvalue()

    def test_a_link_with_no_skills_in_it_says_so(self, tmp_path, monkeypatch):
        (tmp_path / "empty").mkdir()
        self.patch_source(monkeypatch, tmp_path / "empty")
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        run_command(app, "/skills install https://github.com/o/r")
        assert "No skills found" in out.getvalue()

    def test_a_refused_link_is_reported_not_raised(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        run_command(app, "/skills install https://evil.example/x/y")
        assert "Only https://github.com" in out.getvalue()

    def test_text_in_a_downloaded_skill_cannot_draw_on_the_screen(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo" / "s"
        repo.mkdir(parents=True)
        (repo / "SKILL.md").write_text(skill_text("s", "[red]x[/red] \x1b[2J gone"), encoding="utf-8")
        self.patch_source(monkeypatch, tmp_path / "repo")
        app, out, cwd, home = make_app(tmp_path, Lines("n"), MockModel())
        run_command(app, "/skills install https://github.com/o/r")
        assert "\x1b" not in out.getvalue() and "[red]x[/red]" in out.getvalue()

    def test_remove_asks_first_and_deletes_only_a_user_skill(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines("n", "y"), MockModel())
        runner_of(app)
        run_command(app, "/skills remove running-code")
        assert (skills_dir(home) / "running-code").exists()
        run_command(app, "/skills remove running-code")
        assert not (skills_dir(home) / "running-code").exists()
        runner_of(app)                                         # a deleted built-in skill stays deleted
        assert not (skills_dir(home) / "running-code").exists()

    def test_remove_refuses_a_project_skill_and_an_unknown_name(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        (cwd / ".agentx" / "skills" / "house").mkdir(parents=True)
        (cwd / ".agentx" / "skills" / "house" / "SKILL.md").write_text(skill_text("house"), encoding="utf-8")
        app.skills = type(app.skills)(skills_dir(home), project_dirs=[cwd / ".agentx" / "skills"])
        run_command(app, "/skills remove house")
        run_command(app, "/skills remove nothing-here")
        text = out.getvalue()
        assert "belongs to this project" in text and "No skill named 'nothing-here'" in text
        assert (cwd / ".agentx" / "skills" / "house" / "SKILL.md").exists()


class TestSkillsAsCommands:
    """Typing ``/`` offers the skills next to the commands; ``/<skill> [what you want]`` makes the agent use one."""

    def app_with_sends(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=400)     # wide: no line wrapping
        sent = []
        app.send = lambda text, log_title=None: sent.append((text, log_title))
        runner_of(app)                                  # the conversation's runner: this decides which skills apply
        return app, out, cwd, home, sent

    def test_the_popup_entries_are_the_skills_this_conversation_has(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        entries = app.skill_entries()
        assert set(entries) == {"/exploring-a-project", "/changing-files", "/running-code", "/delegating-work"}
        assert entries["/running-code"].startswith("(skill) ")      # web-research has no tools here, so it is not offered

    def test_a_skill_with_a_task_sends_a_message_naming_it_and_the_task(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        run_command(app, "/changing-files rename helper to util everywhere")
        text, title = sent[0]
        assert 'use_skill' in text and '"changing-files"' in text and text.endswith("rename helper to util everywhere")
        assert title == "/changing-files rename helper to util everywhere"

    def test_a_skill_with_no_task_asks_what_to_apply_it_to(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        run_command(app, "/running-code")
        assert 'use_skill' in sent[0][0] and "ask me" in sent[0][0]

    def test_an_unknown_name_sends_nothing_and_says_so(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        run_command(app, "/web-research find the docs")           # a skill that exists, but not for this conversation
        run_command(app, "/nope")
        assert sent == [] and out.getvalue().count("Unknown command") == 2

    def test_a_skill_named_like_a_command_is_reached_with_skills_use(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        (skills_dir(home) / "model").mkdir(parents=True)
        (skills_dir(home) / "model" / "SKILL.md").write_text(skill_text("model", "A skill named like a command"),
                                                              encoding="utf-8")
        sent = []
        app.send = lambda text, log_title=None: sent.append((text, log_title))
        runner_of(app)
        assert "/model" not in app.skill_entries()                # the popup shows the command, not the skill
        run_command(app, "/skills use model tidy up")
        assert '"model"' in sent[0][0] and sent[0][0].endswith("tidy up")

    def test_a_skill_installed_after_the_conversation_began_waits_for_a_new_one(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        (skills_dir(home) / "late-one").mkdir(parents=True)
        (skills_dir(home) / "late-one" / "SKILL.md").write_text(skill_text("late-one"), encoding="utf-8")
        run_command(app, "/late-one")
        assert sent == [] and "late-one" not in app.skill_entries()
        run_command(app, "/skills")
        assert "late-one" in out.getvalue() and "not active in this conversation" in out.getvalue()
        runner_of(app)                                             # what /clear does
        assert "/late-one" in app.skill_entries()

    def test_the_list_shows_how_to_use_each_skill(self, tmp_path):
        app, out, cwd, home, sent = self.app_with_sends(tmp_path)
        run_command(app, "/skills")
        text = out.getvalue()
        assert "/running-code (bundled)" in text and "web-research (bundled)" in text
        assert "not active in this conversation" in text.split("web-research")[1].split("\n")[0]
        assert "/<skill> [what you want]" in text
