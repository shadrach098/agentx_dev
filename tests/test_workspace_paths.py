"""Workspace-rooted paths and the run_python working directory.

Reproduces a user report: a workspace folder holds ``bruce.jpeg``, the
model is told the file is at ``/bruce.jpeg``, and the process was launched
from a different directory. Before the fix the path resolved to the drive
root (``C:\\bruce.jpeg`` on Windows) and run_python started in the
launch directory, so neither the file tools nor Python could find it.
"""

import os

import pytest

from agentx_dev import DefaultTools, Permissions
from agentx_dev.DefaultTools import _resolve_for_ops


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    ws = tmp_path / "my_workspace"
    ws.mkdir()
    (ws / "bruce.jpeg").write_bytes(b"\xff\xd8\xff JPEG")
    launched = tmp_path / "launched_from_here"
    launched.mkdir()
    monkeypatch.chdir(launched)
    perms = Permissions.full_access([str(ws)])
    return ws, perms


def _same(a, b):
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


@pytest.mark.parametrize("form", ["/bruce.jpeg", "\\bruce.jpeg", "bruce.jpeg", "./bruce.jpeg"])
def test_every_path_style_reaches_the_workspace_file(scenario, form):
    ws, perms = scenario
    assert _same(_resolve_for_ops(form, perms), ws / "bruce.jpeg")


def test_real_absolute_path_inside_sandbox_is_unchanged(scenario):
    ws, perms = scenario
    target = str(ws / "bruce.jpeg")
    assert _same(_resolve_for_ops(target, perms), target)


@pytest.mark.parametrize("evil", [
    "/../../etc/passwd",
    "/../launched_from_here/x.txt",
])
def test_rerooting_cannot_escape_the_sandbox(scenario, evil):
    _, perms = scenario
    with pytest.raises(PermissionError):
        _resolve_for_ops(evil, perms)


def test_a_backslash_traversal_cannot_escape_the_sandbox(scenario):
    """On Windows a backslash separates folders, so the climb is refused. On POSIX a
    backslash is an ordinary filename character: the path is just an odd file name
    inside the workspace, which is not an escape either."""
    ws, perms = scenario
    evil = "\\..\\..\\Windows\\win.ini"
    if os.name == "nt":
        with pytest.raises(PermissionError):
            _resolve_for_ops(evil, perms)
    else:
        resolved = os.path.realpath(str(_resolve_for_ops(evil, perms)))
        root = os.path.realpath(str(ws))
        assert os.path.commonpath([resolved, root]) == root


def test_no_workspace_means_no_rerooting(tmp_path):
    perms = Permissions(allowed_paths=[str(tmp_path)], read_files=True)
    with pytest.raises(PermissionError):
        _resolve_for_ops("/bruce.jpeg", perms)


def test_run_python_starts_in_the_workspace(scenario):
    ws, perms = scenario
    tools = {t.name: t for t in DefaultTools.build(perms)}
    out = tools["run_python"].func(
        code="import os; print(os.getcwd()); print(os.path.exists('bruce.jpeg'))"
    )
    lines = [l.strip() for l in str(out).strip().splitlines()]
    assert any(_same(l, ws) for l in lines), out
    assert "True" in lines, out


class TestRootedSandboxRoot:
    r"""Defining the sandbox itself with a rooted path.

    User report: ``Permissions.full_access(["/workspace"])`` was launched
    from a project that holds ``workspace/spam.csv``. On Windows a rooted
    path with no drive letter is the root of the current drive, so the
    sandbox became ``C:\workspace`` -- an empty directory the framework
    created itself. The agent searched it, found nothing, and reported the
    file missing. A leading slash already means "rooted at the workspace"
    for tool arguments, so it must mean "rooted at the project" here.
    """

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "spam.csv").write_text("region,total\nNorth,1\n")
        monkeypatch.chdir(tmp_path)
        return tmp_path

    @pytest.mark.skipif(os.name != "nt", reason="drive-relative rooted paths are Windows-only")
    def test_rooted_path_means_the_project_folder(self, project):
        perms = Permissions.full_access(["/workspace"])
        assert _same(perms.workspace, project / "workspace")
        assert _same(perms.allowed_paths[0], project / "workspace")

    @pytest.mark.skipif(os.name != "nt", reason="drive-relative rooted paths are Windows-only")
    @pytest.mark.parametrize("form", ["spam.csv", "/spam.csv", "./workspace/spam.csv",
                                      "workspace/spam.csv"])
    def test_the_agent_finds_the_file(self, project, form):
        perms = Permissions.full_access(["/workspace"])
        assert _same(_resolve_for_ops(form, perms), project / "workspace" / "spam.csv")

    @pytest.mark.skipif(os.name != "nt", reason="drive-relative rooted paths are Windows-only")
    def test_nothing_is_created_at_the_drive_root(self, project):
        Permissions.full_access(["/agentx_probe_dir"])
        assert not os.path.exists(os.path.join(os.path.splitdrive(os.getcwd())[0] + os.sep,
                                               "agentx_probe_dir"))

    def test_drive_qualified_path_is_left_alone(self, project, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        perms = Permissions.full_access([str(target)])
        assert _same(perms.workspace, target)

    def test_explicit_relative_path_still_works(self, project):
        perms = Permissions.full_access(["./workspace"])
        assert _same(_resolve_for_ops("spam.csv", perms), project / "workspace" / "spam.csv")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX absolute paths keep their meaning")
    def test_posix_absolute_path_is_not_rerooted(self, project):
        perms = Permissions(workspace="/workspace", allowed_paths=["/workspace"],
                            auto_create_paths=False)
        assert perms.workspace == "/workspace"
