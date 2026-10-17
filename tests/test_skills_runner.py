"""``skills=`` on the runners: the use_skill tool, the index in the system prompt, and a prompt that never changes
between two runners built from the same folder (the provider's prompt cache depends on that)."""

import pytest
from pydantic import BaseModel

from agentx_dev import (
    AgentRunner, AgentType, AsyncAgentRunner, DefaultTools, Permissions, StandardTool, StructuredTool,
)
from agentx_dev.Skills import Skills, attach_skills
from tests.conftest import MockModel, make_final


class _Args(BaseModel):
    name: str


def default_tools(tmp_path):
    return DefaultTools.build(Permissions.read_only([str(tmp_path)]))


def build(tmp_path, skills, tools=None, **kw):
    return AgentRunner(model=MockModel(script=[make_final("done")]), agent=AgentType.ReAct,
                       tools=default_tools(tmp_path) if tools is None else tools, skills=skills,
                       verbose=False, auto_cache=False, **kw)


def use_skill_of(runner):
    return next(t for t in runner.tools if t.name == "use_skill")


class TestAttach:
    def test_adds_the_tool_and_the_index_after_the_callers_own_addendum(self, tmp_path):
        runner = build(tmp_path, Skills(tmp_path / "skills"), system_addendum="ROLE TEXT")
        assert runner.registry.has("use_skill") and "use_skill" in runner._tool_prompt_block
        assert runner.system_addendum.startswith("ROLE TEXT\n\nSkills:")
        assert "- exploring-a-project:" in runner.system_addendum

    def test_only_skills_for_tools_the_runner_has_are_listed(self, tmp_path):
        runner = build(tmp_path, Skills(tmp_path / "skills"))      # read-only tools
        assert "exploring-a-project" in runner.system_addendum
        assert "changing-files" not in runner.system_addendum and "running-code" not in runner.system_addendum
        assert "web-research" not in runner.system_addendum

    def test_nothing_is_added_when_no_skill_applies(self, tmp_path):
        runner = build(tmp_path, Skills(tmp_path / "skills"), tools=[], system_addendum="ROLE")
        assert not runner.registry.has("use_skill") and runner.system_addendum == "ROLE"

    def test_the_agents_own_tools_get_one_generated_skill(self, tmp_path):
        mine = StandardTool(lambda x: x, name="lookup_order", description="Find an order. Takes an id.")
        runner = build(tmp_path, Skills(tmp_path / "skills"), tools=[mine])
        assert "- custom-tools: What the agent's own tools do: lookup_order" in runner.system_addendum
        assert "lookup_order" in use_skill_of(runner).func("custom-tools")

    def test_the_tool_returns_the_skill_text_and_a_helpful_error(self, tmp_path):
        tool = use_skill_of(build(tmp_path, Skills(tmp_path / "skills")))
        assert "list_directory" in tool.func("exploring-a-project")
        missing = tool.func("nope")
        assert missing.startswith("ERROR: no skill named 'nope'") and "exploring-a-project" in missing

    def test_the_model_sees_the_index_in_its_system_prompt(self, tmp_path):
        runner = build(tmp_path, Skills(tmp_path / "skills"))
        runner.invoke("hello")
        system = " ".join(str(m["content"]) for m in runner.model.calls[0] if m["role"] == "system")
        assert "Skills:" in system and "exploring-a-project" in system

    def test_true_means_the_users_default_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        runner = build(tmp_path, True)
        assert (tmp_path / ".agentx" / "skills" / "exploring-a-project" / "SKILL.md").is_file()
        assert "exploring-a-project" in runner.system_addendum

    def test_wrong_values_are_refused(self, tmp_path):
        with pytest.raises(TypeError):
            build(tmp_path, "some/folder")
        taken = StructuredTool(func=lambda name: "x", args_schema=_Args, name="use_skill", description="mine")
        with pytest.raises(ValueError):
            build(tmp_path, Skills(tmp_path / "skills"), tools=[taken])

    def test_a_skills_folder_that_cannot_be_written_does_not_stop_the_runner(self, tmp_path, caplog):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        runner = build(tmp_path, Skills(blocker / "skills"))
        assert runner.registry.has("read_path") and "Could not set up" in caplog.text
        assert "exploring-a-project" not in runner.system_addendum      # nothing was created, so nothing is listed


class TestFailureNeverStopsTheAgent:
    def test_a_failure_while_loading_skills_is_logged_and_the_runner_still_builds(self, tmp_path, monkeypatch, caplog):
        def boom(self, tools):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(Skills, "load", boom)
        runner = build(tmp_path, Skills(tmp_path / "skills"), system_addendum="ROLE")
        assert runner.registry.has("read_path") and not runner.registry.has("use_skill")
        assert runner.system_addendum == "ROLE" and "disk on fire" in caplog.text


class TestStablePrompt:
    def test_two_runners_from_one_folder_have_identical_prompts_and_tool_order(self, tmp_path):
        one = build(tmp_path, Skills(tmp_path / "skills"))
        two = build(tmp_path, Skills(tmp_path / "skills"))
        assert one.system_addendum == two.system_addendum
        assert [t.name for t in one.tools] == [t.name for t in two.tools]
        assert one._tool_prompt_block == two._tool_prompt_block

    def test_building_a_second_runner_rewrites_no_files(self, tmp_path):
        build(tmp_path, Skills(tmp_path / "skills"))
        files = [p for p in (tmp_path / "skills").rglob("*") if p.is_file()]
        before = {p: p.stat().st_mtime_ns for p in files}
        build(tmp_path, Skills(tmp_path / "skills"))
        assert before == {p: p.stat().st_mtime_ns for p in files}

    def test_the_tool_order_does_not_change_the_generated_skill(self, tmp_path):
        a = StandardTool(lambda x: x, name="aaa", description="A.")
        b = StandardTool(lambda x: x, name="bbb", description="B.")
        one = attach_skills(Skills(tmp_path / "s"), [a, b], None)[1]
        two = attach_skills(Skills(tmp_path / "s"), [b, a], None)[1]
        assert one == two

    def test_a_skill_edited_on_disk_after_the_runner_was_built_changes_nothing_for_it(self, tmp_path):
        runner = build(tmp_path, Skills(tmp_path / "skills"))
        before = runner.system_addendum
        (tmp_path / "skills" / "new-one").mkdir()
        (tmp_path / "skills" / "new-one" / "SKILL.md").write_text(
            "---\nname: new-one\ndescription: later\n---\nbody", encoding="utf-8")
        assert runner.system_addendum == before
        assert use_skill_of(runner).func("new-one").startswith("ERROR: no skill named")


class TestAsync:
    def test_the_async_runner_gets_the_same_tool_and_index(self, tmp_path):
        runner = AsyncAgentRunner(model=MockModel(script=[]), agent=AgentType.ReAct, tools=default_tools(tmp_path),
                                  skills=Skills(tmp_path / "skills"), system_addendum="ROLE", verbose=False,
                                  auto_cache=False)
        assert runner.registry.has("use_skill")
        assert runner.system_addendum.startswith("ROLE\n\nSkills:") and "exploring-a-project" in runner.system_addendum


class TestObserver:
    """A caller can be told each time the model opens a skill (the terminal app shows a team helper's reads)."""

    def test_the_observer_hears_each_call_with_its_arguments_and_the_skill_still_comes_back(self, tmp_path):
        seen = []
        runner = build(tmp_path, Skills(tmp_path / "skills", observer=lambda name, args: seen.append((name, args))))
        tool = use_skill_of(runner)
        assert "list_directory" in tool.func("exploring-a-project")
        tool.func("exploring-a-project", "missing.md")
        tool.func("nope")
        assert seen == [("use_skill", {"name": "exploring-a-project"}),
                        ("use_skill", {"name": "exploring-a-project", "file": "missing.md"}),
                        ("use_skill", {"name": "nope"})]

    def test_an_observer_that_fails_never_breaks_the_tool(self, tmp_path):
        def broken(name, args):
            raise RuntimeError("screen gone")
        tool = use_skill_of(build(tmp_path, Skills(tmp_path / "skills", observer=broken)))
        assert "list_directory" in tool.func("exploring-a-project")

    def test_no_observer_is_the_default(self, tmp_path):
        assert Skills(tmp_path / "skills").observer is None


class TestRememberReads:
    """Opt-in: a skill read once in this conversation is not sent again in full, because its text is still in the
    history. Off by default: a runner reused for unrelated histories must not claim a skill was 'already read'."""

    def tool(self, tmp_path, **kw):
        return use_skill_of(build(tmp_path, Skills(tmp_path / "skills", **kw)))

    def test_off_by_default_the_full_text_comes_every_time(self, tmp_path):
        tool = self.tool(tmp_path)
        assert tool.func("exploring-a-project") == tool.func("exploring-a-project")
        assert "list_directory" in tool.func("exploring-a-project")

    def test_with_it_on_the_second_read_is_a_short_pointer_back(self, tmp_path):
        tool = self.tool(tmp_path, remember_reads=True)
        first = tool.func("exploring-a-project")
        second = tool.func("exploring-a-project")
        assert "list_directory" in first and len(first) > 500
        assert len(second) < 300 and "already read" in second and 'file="SKILL.md"' in second
        assert "list_directory" not in second

    def test_the_pointer_tells_how_to_get_the_whole_text_again(self, tmp_path):
        tool = self.tool(tmp_path, remember_reads=True)
        tool.func("exploring-a-project")
        again = tool.func("exploring-a-project", "SKILL.md")
        assert "list_directory" in again and again.startswith("---")        # the file as it is on disk

    def test_each_skill_has_its_own_first_read(self, tmp_path):
        (tmp_path / "skills" / "mine").mkdir(parents=True)
        (tmp_path / "skills" / "mine" / "SKILL.md").write_text(
            "---\nname: mine\ndescription: My own guide\n---\nMY OWN TEXT\n", encoding="utf-8")
        tool = self.tool(tmp_path, remember_reads=True)
        tool.func("exploring-a-project")
        assert "MY OWN TEXT" in tool.func("mine")                      # a different skill is read in full
        assert "already read" in tool.func("mine")

    def test_an_unknown_name_is_not_remembered_and_forget_starts_over(self, tmp_path):
        tool = self.tool(tmp_path, remember_reads=True)
        assert tool.func("nope").startswith("ERROR") and tool.func("nope").startswith("ERROR")
        tool.func("exploring-a-project")
        tool.forget_reads()
        assert "list_directory" in tool.func("exploring-a-project")

    def test_the_observer_still_hears_every_call(self, tmp_path):
        seen = []
        tool = self.tool(tmp_path, remember_reads=True, observer=lambda n, a: seen.append(a["name"]))
        tool.func("exploring-a-project")
        tool.func("exploring-a-project")
        assert seen == ["exploring-a-project", "exploring-a-project"]
