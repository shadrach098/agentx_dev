"""The skills that ship with agentx, and how they are created in the user's skills folder."""

import json
import os
import shutil

import pytest

from agentx_dev import DefaultTools, StandardTool
from agentx_dev.Skills.Defaults import (
    CUSTOM_TOOLS, STATE_FILE, Bundled, bundled_skills, custom_tools_skill, seed_defaults,
)
from agentx_dev.Skills.Store import NAME_RE, SIDECAR, SkillStore, parse_skill_md

SHIPPED_TOOLS = set(DefaultTools.names()) | {"web_search", "web_fetch", "start_team_job", "delegate", "recall"}


def sample(name="demo", version=1, text="Do it well."):
    return Bundled(name, f"---\nname: {name}\ndescription: A demo guide\ntools: [read_path]\nversion: {version}\n---\n{text}\n",
                   version)


class TestShippedSkills:
    def test_they_are_found_through_the_package(self):
        names = [b.name for b in bundled_skills()]
        assert names == sorted(names) and len(names) >= 5

    def test_each_one_is_well_formed(self):
        for b in bundled_skills():
            meta, body = parse_skill_md(b.text)
            assert meta["name"] == b.name and NAME_RE.match(b.name)
            assert 20 <= len(meta["description"]) <= 200, b.name
            assert meta["tools"] and all(isinstance(t, str) for t in meta["tools"]), b.name
            assert isinstance(meta["version"], int) and 100 < len(body) < 4000, b.name

    def test_together_they_cover_every_tool_agentx_ships(self):
        covered = {t for b in bundled_skills() for t in parse_skill_md(b.text)[0]["tools"]}
        assert covered == SHIPPED_TOOLS

    def test_they_load_as_skills_once_seeded(self, tmp_path):
        seed_defaults(tmp_path)
        store = SkillStore([(tmp_path, "user")])
        assert store.problems == [] and {s.source for s in store.all()} == {"bundled"}
        assert len(store.all()) == len(bundled_skills())


class TestSeeding:
    def test_first_run_creates_each_default_with_a_sidecar(self, tmp_path):
        assert seed_defaults(tmp_path, [sample("a"), sample("b")]) == ["a", "b"]
        assert (tmp_path / "a" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: a")
        sidecar = json.loads((tmp_path / "a" / SIDECAR).read_text(encoding="utf-8"))
        assert sidecar["managed"] is True and sidecar["version"] == 1 and len(sidecar["sha256"]) == 64

    def test_a_second_run_changes_nothing_not_even_file_times(self, tmp_path):
        seed_defaults(tmp_path, [sample("a")])
        before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
        assert seed_defaults(tmp_path, [sample("a")]) == []
        assert before == {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    def test_a_default_the_user_deleted_stays_deleted(self, tmp_path):
        seed_defaults(tmp_path, [sample("a")])
        shutil.rmtree(tmp_path / "a")
        assert seed_defaults(tmp_path, [sample("a")]) == [] and not (tmp_path / "a").exists()
        assert "a" in json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["seen"]

    def test_a_new_default_in_a_newer_agentx_is_added(self, tmp_path):
        seed_defaults(tmp_path, [sample("a")])
        assert seed_defaults(tmp_path, [sample("a"), sample("b")]) == ["b"]

    def test_an_untouched_copy_is_updated_when_the_shipped_version_goes_up(self, tmp_path):
        seed_defaults(tmp_path, [sample("a", 1, "old text")])
        assert seed_defaults(tmp_path, [sample("a", 2, "new text")]) == ["a"]
        assert "new text" in (tmp_path / "a" / "SKILL.md").read_text(encoding="utf-8")
        assert json.loads((tmp_path / "a" / SIDECAR).read_text(encoding="utf-8"))["version"] == 2

    def test_a_copy_the_user_edited_is_never_overwritten(self, tmp_path):
        seed_defaults(tmp_path, [sample("a", 1)])
        (tmp_path / "a" / "SKILL.md").write_text("---\nname: a\ndescription: mine\n---\nmy own text\n", encoding="utf-8")
        assert seed_defaults(tmp_path, [sample("a", 2)]) == []
        assert "my own text" in (tmp_path / "a" / "SKILL.md").read_text(encoding="utf-8")

    def test_a_skill_the_user_made_under_the_same_name_is_left_alone(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "SKILL.md").write_text("---\nname: a\ndescription: mine\n---\nmine\n", encoding="utf-8")
        assert seed_defaults(tmp_path, [sample("a")]) == []
        assert not (tmp_path / "a" / SIDECAR).exists()

    def test_a_folder_that_cannot_be_written_does_not_raise(self, tmp_path, caplog):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        assert seed_defaults(blocker / "skills", [sample("a")]) == []
        assert "Could not" in caplog.text


class TestCustomToolsSkill:
    def tool(self, name, description="Does the thing. More detail here."):
        return StandardTool(lambda x: x, name=name, description=description)

    def test_none_when_every_tool_is_covered(self):
        assert custom_tools_skill([self.tool("a")], {"a"}) is None

    def test_describes_only_the_uncovered_tools_sorted_and_never_use_skill(self):
        skill = custom_tools_skill([self.tool("zed"), self.tool("alpha"), self.tool("use_skill"), self.tool("read_path")],
                                   {"read_path"})
        assert skill.name == CUSTOM_TOOLS and skill.source == "generated" and skill.folder is None
        assert skill.description.endswith("alpha, zed")
        assert skill.body.index("## alpha") < skill.body.index("## zed") and "read_path" not in skill.body

    def test_the_same_tools_in_any_order_give_the_same_text(self):
        one = custom_tools_skill([self.tool("b"), self.tool("a")], set())
        two = custom_tools_skill([self.tool("a"), self.tool("b")], set())
        assert one == two

    def test_shows_argument_names_when_the_tool_has_a_schema(self, tmp_path):
        from agentx_dev import Permissions
        tools = DefaultTools.build(Permissions.read_only([str(tmp_path)]))
        grep = next(t for t in tools if t.name == "grep")
        assert "pattern" in custom_tools_skill([grep], set()).body
