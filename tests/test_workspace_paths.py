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
    "\\..\\..\\Windows\\win.ini",
    "/../launched_from_here/x.txt",
])
def test_rerooting_cannot_escape_the_sandbox(scenario, evil):
    _, perms = scenario
    with pytest.raises(PermissionError):
        _resolve_for_ops(evil, perms)


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
