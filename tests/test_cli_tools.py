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


def test_all_twelve_tools_are_built_and_never_cached(tmp_path):
    tools = build_tools(tmp_path, [], Gate())
    assert sorted(t.name for t in tools) == sorted([
        "read_path", "list_directory", "find_files", "grep", "write_file", "edit_file",
        "delete_path", "move_file", "run_python", "run_shell", "web_search", "web_fetch"])
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
    # the framework's tool raises PermissionError (its runner turns that into a tool error for the model).
    # "../outside.txt" leaves the project on every platform; an absolute "/tmp/..." path would not on POSIX,
    # where a leading "/" means the workspace root (3.6).
    with pytest.raises(PermissionError, match="outside the allowed sandbox"):
        tools["write_file"].func(path="../outside.txt", content="x", if_exists="refuse")
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
    from agentx_dev.cli.tools import web_tools
    originals = tools_by_name([*DefaultTools.build(permissions), *web_tools()])
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


# --- the gate judges the path the tool will really touch ---------------------------------------------------

class Recording(Gate):
    """A real gate that remembers the arguments it was asked about."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen = []

    def check(self, tool, args):
        self.seen.append((tool, dict(args)))
        return super().check(tool, args)


class SubjectAsker:
    def __init__(self, choice="n"):
        self.choice, self.subjects = choice, []

    def __call__(self, request):
        self.subjects.append((request.tool, request.subject))
        return Answer(self.choice)


def _project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    return project


@pytest.mark.parametrize("spelling", ["/secrets/k.txt", "\\secrets\\k.txt"])
def test_a_rooted_path_is_gated_as_the_file_inside_the_project_the_tool_will_write(tmp_path, spelling):
    project = _project(tmp_path)
    gate = Gate(mode="accept-edits", deny=["write_file:secrets/*", "write_file:.env"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    result = tools["write_file"].func(path=spelling, content="PWNED", if_exists="overwrite")
    assert "deny rule" in result
    assert not (project / "secrets" / "k.txt").exists()


@pytest.mark.parametrize("spelling", ["/.env", "\\.env"])
def test_a_rooted_dotenv_write_is_denied(tmp_path, spelling):
    project = _project(tmp_path)
    gate = Gate(mode="accept-edits", deny=["write_file:.env"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["write_file"].func(path=spelling, content="PWNED", if_exists="overwrite")
    assert not (project / ".env").exists()


def test_a_rooted_read_edit_delete_and_move_are_denied_like_the_real_file(tmp_path):
    project = _project(tmp_path)
    (project / ".env").write_text("SECRET", encoding="utf-8")
    (project / "secrets").mkdir()
    gate = Gate(mode="accept-edits", cwd=project, ask=SubjectAsker("y"),
                deny=["read_path:.env", "edit_file:.env", "delete_path:secrets", "move_file:*.env*"])
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["read_path"].func(path="/.env")
    assert "deny rule" in tools["edit_file"].func(path="/.env", find="SECRET", replace="X")
    assert "deny rule" in tools["delete_path"].func(path="/secrets", recursive=True)
    assert "deny rule" in tools["move_file"].func(source="/.env", destination="stolen.txt")
    assert (project / ".env").read_text() == "SECRET" and (project / "secrets").is_dir()
    assert not (project / "stolen.txt").exists()
    assert "deny rule" in tools["move_file"].func(source="ok.txt", destination="/.env")


def test_the_prompt_names_the_real_file_for_a_rooted_path(tmp_path):
    project = _project(tmp_path)
    (project / "secrets").mkdir()
    asker = SubjectAsker("n")
    tools = tools_by_name(build_tools(project, [], Gate(cwd=project, ask=asker)))
    tools["delete_path"].func(path="/secrets", recursive=True)
    tools["write_file"].func(path="/notes/a.txt", content="x", if_exists="refuse")
    assert asker.subjects == [("delete_path", "secrets"), ("write_file", "notes/a.txt")]
    assert (project / "secrets").is_dir()


def test_the_gate_and_the_tool_get_the_same_reroot_of_a_rooted_path(tmp_path):
    project = _project(tmp_path)
    gate = Recording(mode="accept-edits", cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    tools["write_file"].func(path="/sub/a.txt", content="x", if_exists="refuse")
    assert (project / "sub" / "a.txt").read_text() == "x"
    assert gate.seen[0][1]["path"] == "sub/a.txt"          # the project-relative text, not a resolved path


@pytest.mark.parametrize("spelling", [
    "secrets/a.txt", "./secrets/a.txt", "sub/../secrets/a.txt", "../proj/secrets/a.txt", "SECRETS_ABS"])
def test_ordinary_spellings_are_still_denied(tmp_path, spelling):
    project = _project(tmp_path)
    if spelling == "SECRETS_ABS":
        spelling = str(project / "secrets" / "a.txt")
    gate = Gate(mode="accept-edits", deny=["write_file:secrets/*"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["write_file"].func(path=spelling, content="x", if_exists="refuse")
    assert not (project / "secrets").exists()


@pytest.mark.parametrize("spelling", ["ok.txt", "./ok.txt", "sub/../ok.txt"])
def test_ordinary_spellings_still_run_when_allowed(tmp_path, spelling):
    project = _project(tmp_path)
    gate = Gate(mode="accept-edits", deny=["write_file:secrets/*"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    tools["write_file"].func(path=spelling, content="x", if_exists="refuse")
    assert (project / "ok.txt").read_text() == "x"


def test_a_path_in_an_added_folder_is_gated_as_that_file(tmp_path):
    project, extra = _project(tmp_path), tmp_path / "extra"
    extra.mkdir()
    gate = Gate(mode="accept-edits", deny=[f"write_file:{(extra / 'k.txt').as_posix()}"], cwd=project)
    tools = tools_by_name(build_tools(project, [extra], gate))
    assert "deny rule" in tools["write_file"].func(path=str(extra / "k.txt"), content="x", if_exists="refuse")
    assert not (extra / "k.txt").exists()


def test_a_path_the_tool_rejects_reaches_the_gate_as_written_and_the_tool_still_raises(tmp_path):
    project = _project(tmp_path)
    outside = tmp_path / "outside.txt"
    gate = Recording(mode="accept-edits", cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    with pytest.raises(PermissionError):
        tools["write_file"].func(path="../outside.txt", content="x", if_exists="refuse")
    assert gate.seen[0][1]["path"] == "../outside.txt" and not outside.exists()


def test_run_shell_cwd_is_rerooted_like_a_path(tmp_path):
    project = _project(tmp_path)
    (project / "sub").mkdir()
    gate = Recording(mode="accept-edits", cwd=project, ask=SubjectAsker("n"))
    tools = tools_by_name(build_tools(project, [], gate))
    tools["run_shell"].func(command="echo hi", cwd="/sub")
    assert gate.seen[0][1]["cwd"] == "sub"
    tools["run_shell"].func(command="echo hi")                       # no cwd: nothing to rewrite
    assert gate.seen[1][1]["cwd"] is None


# --- one prompt at a time, per-tool identity, no folder creation ------------------------------------------

def test_approvals_are_serialised_across_parallel_calls(tmp_path):
    import threading
    import time
    active, worst, lock = [0], [0], threading.Lock()

    def slow_ask(request):
        with lock:
            active[0] += 1
            worst[0] = max(worst[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        return Answer("y")

    tools = tools_by_name(build_tools(tmp_path, [], Gate(cwd=tmp_path, ask=slow_ask)))
    threads = [threading.Thread(target=tools["write_file"].func,
                                kwargs=dict(path=f"f{i}.txt", content="x", if_exists="refuse"))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert worst[0] == 1 and all((tmp_path / f"f{i}.txt").exists() for i in range(3))


def test_the_tool_itself_is_not_run_under_the_approval_lock(tmp_path):
    import threading
    gate = _Recorder()
    tool, _ = _spy_tool()
    entered, release = threading.Event(), threading.Event()

    def slow(**kwargs):
        entered.set()
        release.wait(2)
        return "done"

    slow_tool = StructuredTool(func=slow, args_schema=_SpyArgs, name="slow_tool", description="slow")
    lock = threading.Lock()
    first = gate_tool(slow_tool, gate, lock=lock)
    second = gate_tool(tool, gate, lock=lock)
    thread = threading.Thread(target=first.func, kwargs=dict(path="a"))
    thread.start()
    assert entered.wait(2)
    assert lock.acquire(timeout=1)          # the first call is mid-run, yet the lock is free
    lock.release()
    assert second.func(path="b") == "ran"
    release.set()
    thread.join()


def test_each_wrapped_tool_has_its_own_function_identity(tmp_path):
    tools = build_tools(tmp_path, [], Gate())
    assert len({t.func.__qualname__ for t in tools}) == len(tools)
    assert all(t.name in t.func.__qualname__ for t in tools)


def test_a_mistyped_added_folder_is_not_created(tmp_path):
    project, typo = _project(tmp_path), tmp_path / "typo"
    tools = tools_by_name(build_tools(project, [typo], Gate(cwd=project)))
    assert not typo.exists() and "write_file" in tools


# --- link names stay visible to the gate; only a re-rooted path is rewritten --------------------------------

import os
import sys

ON_WINDOWS = sys.platform == "win32"


def make_link(link, target):
    """A directory junction on Windows, a symlink elsewhere; skips the test when the OS will not make one."""
    try:
        if ON_WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, ImportError, AttributeError, NotImplementedError) as error:
        pytest.skip(f"cannot create a link here: {error}")


def make_file_link(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create a file symlink here: {error}")


def _linked_project(tmp_path):
    project = _project(tmp_path)
    (project / "secrets").mkdir()
    (project / "secrets" / "k.txt").write_text("KEY", encoding="utf-8")
    make_link(project / "link", project / "secrets")
    return project


def test_a_deny_rule_on_a_link_name_blocks_write_file_through_the_link(tmp_path):
    project = _linked_project(tmp_path)
    gate = Gate(mode="accept-edits", deny=["write_file:link/*"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["write_file"].func(path="link/new.txt", content="x", if_exists="refuse")
    assert not (project / "secrets" / "new.txt").exists()


def test_a_deny_rule_on_a_link_name_blocks_read_edit_delete_and_move(tmp_path):
    project = _linked_project(tmp_path)
    gate = Gate(mode="accept-edits", cwd=project, ask=SubjectAsker("y"),
                deny=["read_path:link/*", "edit_file:link/*", "delete_path:link/*", "move_file:link/*"])
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["read_path"].func(path="link/k.txt")
    assert "deny rule" in tools["edit_file"].func(path="link/k.txt", find="KEY", replace="X")
    assert "deny rule" in tools["delete_path"].func(path="link/k.txt")
    assert "deny rule" in tools["move_file"].func(source="link/k.txt", destination="moved.txt")
    assert (project / "secrets" / "k.txt").read_text() == "KEY" and not (project / "moved.txt").exists()


def test_the_gate_sees_a_link_path_as_the_model_wrote_it(tmp_path):
    project = _linked_project(tmp_path)
    gate = Recording(mode="accept-edits", cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    tools["read_path"].func(path="link/k.txt")
    assert gate.seen[0][1]["path"] == "link/k.txt"


def test_a_dotenv_symlinked_to_another_file_is_still_denied_by_name(tmp_path):
    project = _project(tmp_path)
    (project / "config").mkdir()
    (project / "config" / "prod.env").write_text("SECRET", encoding="utf-8")
    make_file_link(project / ".env", project / "config" / "prod.env")
    gate = Gate(mode="accept-edits", deny=["read_path:.env", "edit_file:.env"], cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    assert "deny rule" in tools["read_path"].func(path=".env")
    assert "deny rule" in tools["edit_file"].func(path=".env", find="SECRET", replace="X")
    assert (project / "config" / "prod.env").read_text() == "SECRET"


def test_a_rooted_path_inside_the_sandbox_is_left_as_written(tmp_path):
    project = _project(tmp_path)
    rooted = (project / "a.txt").as_posix()
    if ON_WINDOWS:
        rooted = rooted[2:]                        # no drive letter: rooted on the current drive
    gate = Recording(mode="accept-edits", cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    tools["write_file"].func(path=rooted, content="x", if_exists="refuse")
    assert gate.seen[0][1]["path"] == rooted and (project / "a.txt").read_text() == "x"


def test_a_path_with_a_drive_or_relative_is_left_as_written(tmp_path):
    project = _project(tmp_path)
    gate = Recording(mode="accept-edits", cwd=project)
    tools = tools_by_name(build_tools(project, [], gate))
    absolute = str(project / "a.txt")
    tools["write_file"].func(path=absolute, content="x", if_exists="refuse")
    tools["write_file"].func(path="./b.txt", content="x", if_exists="refuse")
    assert [seen[1]["path"] for seen in gate.seen] == [absolute, "./b.txt"]


def test_the_error_text_for_a_relative_path_is_what_the_tool_produced(tmp_path):
    project = _project(tmp_path)
    tools = tools_by_name(build_tools(project, [], Gate(mode="accept-edits", cwd=project)))
    result = tools["edit_file"].func(path="missing.txt", find="a", replace="b")
    assert "missing.txt" in result and str(project) not in result.replace(str(project / "missing.txt"), "")


def test_two_tool_sets_sharing_one_gate_share_one_approval_lock(tmp_path):
    import threading
    import time
    active, worst, lock = [0], [0], threading.Lock()

    def slow_ask(request):
        with lock:
            active[0] += 1
            worst[0] = max(worst[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        return Answer("y")

    gate = Gate(cwd=tmp_path, ask=slow_ask)
    first = tools_by_name(build_tools(tmp_path, [], gate))
    second = tools_by_name(build_tools(tmp_path, [], gate))
    threads = [threading.Thread(target=t["write_file"].func,
                                kwargs=dict(path=f"g{i}.txt", content="x", if_exists="refuse"))
               for i, t in enumerate([first, second])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert worst[0] == 1 and (tmp_path / "g0.txt").exists() and (tmp_path / "g1.txt").exists()


def test_python_and_shell_get_longer_limits_than_the_framework_defaults():
    from agentx_dev.cli.tools import PYTHON_TIMEOUT_SEC, SHELL_TIMEOUT_SEC
    assert PYTHON_TIMEOUT_SEC >= 60 and SHELL_TIMEOUT_SEC >= 120      # the framework's own: 10 s and 30 s


class TestObserver:
    def tools(self, tmp_path, observer, **gate_kw):
        gate = Gate(cwd=tmp_path, **gate_kw)
        return gate, {t.name: t.func for t in build_tools(tmp_path, [], gate, observer=observer)}

    def test_the_observer_sees_each_call_before_the_gate_decides(self, tmp_path):
        order = []
        gate, tools = self.tools(tmp_path, lambda name, args: order.append(("seen", name, dict(args))))
        real = gate.check

        def spy(tool, args):
            order.append(("gate", tool))
            return real(tool, args)

        gate.check = spy
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        assert "x" in tools["read_path"](path="a.txt")
        assert order[0][:2] == ("seen", "read_path") and order[0][2]["path"] == "a.txt"
        assert order[1] == ("gate", "read_path")

    def test_it_also_sees_calls_the_gate_refuses(self, tmp_path):
        seen = []
        gate, tools = self.tools(tmp_path, lambda name, args: seen.append(name), mode="plan")
        answer = tools["write_file"](path="b.txt", content="x", if_exists="refuse")
        assert seen == ["write_file"] and "plan mode" in answer
        assert not (tmp_path / "b.txt").exists()

    def test_an_observer_that_raises_changes_nothing(self, tmp_path):
        def boom(name, args):
            raise RuntimeError("display failed")

        _, tools = self.tools(tmp_path, boom)
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        assert "hello" in tools["read_path"](path="a.txt")

    def test_the_observer_gets_a_copy_it_cannot_use_to_change_the_call(self, tmp_path):
        def tamper(name, args):
            args["path"] = "other.txt"

        _, tools = self.tools(tmp_path, tamper)
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "other.txt").write_text("WRONG", encoding="utf-8")
        assert "hello" in tools["read_path"](path="a.txt")

    def test_invalid_arguments_are_not_shown_to_the_observer(self, tmp_path):
        seen = []
        _, tools = self.tools(tmp_path, lambda name, args: seen.append(name))
        assert tools["read_path"]().startswith("ERROR: invalid arguments")
        assert seen == []
