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
