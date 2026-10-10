"""The sandboxed tool set with the gate in front of it."""

from pathlib import Path
from typing import Optional

import pytest
from pydantic import BaseModel, Field

from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Answer, Gate
from agentx_dev.cli.tools import build_tools, gate_tool


def tools_by_name(tools):
    return {t.name: t for t in tools}


class Asker:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, request):
        self.seen.append(request.tool)
        return self.answers.pop(0) if self.answers else Answer("y")


def test_all_ten_tools_are_built_and_never_cached(tmp_path):
    tools = build_tools(tmp_path, [], Gate())
    assert sorted(t.name for t in tools) == sorted([
        "read_path", "list_directory", "find_files", "grep", "write_file", "edit_file",
        "delete_path", "move_file", "run_python", "run_shell"])
    assert all(t.cacheable is False for t in tools)
    assert all(t.args_schema is not None and t.description for t in tools)


def test_an_approved_write_happens_and_a_declined_one_does_not(tmp_path):
    asker = Asker(Answer("y"), Answer("n", "not now"))
    tools = tools_by_name(build_tools(tmp_path, [], Gate(ask=asker)))
    write = tools["write_file"].func
    first = write(path="a.txt", content="hi", if_exists="refuse")
    assert (tmp_path / "a.txt").read_text() == "hi" and "ERROR" not in str(first)
    second = write(path="b.txt", content="no", if_exists="refuse")
    assert second == "The user declined: not now" and not (tmp_path / "b.txt").exists()
    assert asker.seen == ["write_file", "write_file"]


def test_reads_never_ask(tmp_path):
    (tmp_path / "f.txt").write_text("content", encoding="utf-8")
    asker = Asker()
    tools = tools_by_name(build_tools(tmp_path, [], Gate(ask=asker)))
    assert "content" in str(tools["read_path"].func(path="f.txt"))
    assert asker.seen == []


def test_plan_mode_blocks_a_write_without_asking(tmp_path):
    asker = Asker()
    tools = tools_by_name(build_tools(tmp_path, [], Gate(mode="plan", ask=asker)))
    result = tools["write_file"].func(path="a.txt", content="x", if_exists="refuse")
    assert "plan mode" in result and not (tmp_path / "a.txt").exists() and asker.seen == []


def test_a_write_outside_the_sandbox_is_refused_by_the_tool_even_when_approved(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    tools = tools_by_name(build_tools(project, [], Gate(mode="accept-edits")))
    # the framework's tool raises PermissionError (its runner turns that into a tool error for the model)
    with pytest.raises(PermissionError, match="outside the allowed sandbox"):
        tools["write_file"].func(path=str(outside), content="x", if_exists="refuse")
    assert not outside.exists()


def test_an_added_folder_is_inside_the_sandbox(tmp_path):
    project, extra = tmp_path / "proj", tmp_path / "extra"
    project.mkdir()
    extra.mkdir()
    tools = tools_by_name(build_tools(project, [extra], Gate(mode="accept-edits")))
    tools["write_file"].func(path=str(extra / "ok.txt"), content="x", if_exists="refuse")
    assert (extra / "ok.txt").read_text() == "x"


def test_relative_paths_land_in_the_project_folder(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    tools = tools_by_name(build_tools(project, [], Gate(mode="accept-edits")))
    tools["write_file"].func(path="rel.txt", content="x", if_exists="refuse")
    assert (project / "rel.txt").exists() and not Path("rel.txt").exists()


# --- gate_tool in isolation: a spy tool shows exactly what is allowed to run -----------------------------

class _SpyArgs(BaseModel):
    """Spy arguments."""
    path: str = Field(..., description="a path")
    count: int = Field(1, description="a number")
    flag: Optional[bool] = Field(False, description="a flag")


class _Recorder:
    """A gate stand-in that records what it was asked and answers as told."""

    def __init__(self, verdict=None, error=None):
        self.verdict, self.error, self.calls = verdict, error, []

    def check(self, tool, args):
        self.calls.append((tool, dict(args)))
        if self.error is not None:
            raise self.error
        return self.verdict


def _spy_tool():
    ran = []

    def spy(**kwargs):
        ran.append(kwargs)
        return "ran"

    tool = StructuredTool(func=spy, args_schema=_SpyArgs, name="spy_tool", description="Spy on calls.")
    return tool, ran


def test_gate_tool_keeps_name_description_and_schema():
    tool, _ = _spy_tool()
    wrapped = gate_tool(tool, _Recorder())
    assert wrapped is not tool
    assert (wrapped.name, wrapped.description, wrapped.args_schema) == (tool.name, tool.description, tool.args_schema)
    assert wrapped.to_openai_tool() == tool.to_openai_tool()
    assert wrapped.to_anthropic_tool() == tool.to_anthropic_tool()


def test_every_real_tool_keeps_its_schema_description_and_name(tmp_path):
    from agentx_dev.DefaultTools import DefaultTools, Permissions
    permissions = Permissions(read_files=True, list_directories=True, write_files=True, edit_files=True,
                              delete_files=True, move_files=True, execute_python=True, execute_shell=True,
                              workspace=str(tmp_path), allowed_paths=[str(tmp_path)])
    originals = tools_by_name(DefaultTools.build(permissions))
    wrapped = tools_by_name(build_tools(tmp_path, [], Gate()))
    assert set(originals) == set(wrapped)
    for name, tool in wrapped.items():
        assert (tool.description, tool.args_schema) == (originals[name].description, originals[name].args_schema)
        assert tool.to_openai_tool() == originals[name].to_openai_tool()


def test_a_refusal_is_returned_and_the_tool_never_runs():
    tool, ran = _spy_tool()
    gate = _Recorder(verdict="Refused: nope")
    assert gate_tool(tool, gate).func(path="x") == "Refused: nope"
    assert ran == [] and len(gate.calls) == 1


def test_a_gate_that_raises_fails_closed():
    tool, ran = _spy_tool()
    result = gate_tool(tool, _Recorder(error=RuntimeError("boom"))).func(path="x")
    assert ran == [] and isinstance(result, str) and result.startswith("Refused")
    assert "boom" not in result


def test_a_positional_call_cannot_slip_past_the_gate():
    tool, ran = _spy_tool()
    gate = _Recorder()
    wrapped = gate_tool(tool, gate)
    with pytest.raises(TypeError):
        wrapped.func("x")
    assert ran == [] and gate.calls == []


def test_the_gate_sees_what_the_tool_receives_defaults_and_coercion_included():
    tool, ran = _spy_tool()
    gate = _Recorder()
    assert gate_tool(tool, gate).func(path="x", count="3") == "ran"
    assert gate.calls == [("spy_tool", {"path": "x", "count": 3, "flag": False})]
    assert ran == [{"path": "x", "count": 3, "flag": False}]


def test_unknown_arguments_are_dropped_for_both_the_gate_and_the_tool():
    tool, ran = _spy_tool()
    gate = _Recorder()
    gate_tool(tool, gate).func(path="x", sneaky="--force")
    assert "sneaky" not in gate.calls[0][1] and "sneaky" not in ran[0]


def test_invalid_arguments_run_nothing():
    tool, ran = _spy_tool()
    gate = _Recorder()
    result = gate_tool(tool, gate).func(count=2)             # path is missing
    assert ran == [] and gate.calls == [] and isinstance(result, str) and result.startswith("ERROR")


def test_a_real_tool_is_gated_on_the_coerced_arguments(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    seen = []

    class Spy(Gate):
        def check(self, tool, args):
            seen.append((tool, dict(args)))
            return super().check(tool, args)

    tools = tools_by_name(build_tools(tmp_path, [], Spy()))
    assert "b" in str(tools["read_path"].func(path="f.txt", offset="2", limit="1"))
    assert seen == [("read_path", {"path": "f.txt", "offset": 2, "limit": 1})]
