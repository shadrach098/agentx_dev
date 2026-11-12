"""safe_exe never picks a program from the current (project) folder."""

import os
import sys

import pytest

from agentx_dev import safe_exe


def _make_program(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + (".cmd" if sys.platform == "win32" else ""))
    path.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_program_in_current_folder_is_never_found(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _make_program(project, "fakeprog")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(project)]))
    assert safe_exe.which("fakeprog") is None


def test_program_on_path_is_found_by_absolute_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    real = _make_program(tmp_path / "bin", "fakeprog")
    _make_program(project, "fakeprog")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "bin")]))
    found = safe_exe.which("fakeprog")
    assert found is not None and os.path.isabs(found)
    assert os.path.samefile(found, real)


def test_path_entry_inside_project_is_skipped(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _make_program(project / "tools", "fakeprog")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", str(project / "tools"))
    assert safe_exe.which("fakeprog") is None


def test_names_with_a_folder_are_not_looked_up(tmp_path):
    assert safe_exe.which(os.path.join("sub", "prog")) is None
    assert safe_exe.which("") is None


@pytest.mark.skipif(sys.platform != "win32", reason="System32 paths are Windows only")
def test_system32_paths_are_absolute():
    assert os.path.isabs(safe_exe.system32("taskkill.exe"))
    assert safe_exe.powershell().lower().endswith(os.path.join("windowspowershell", "v1.0", "powershell.exe"))
