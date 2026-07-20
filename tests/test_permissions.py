"""Tests for Permissions + DefaultTools capability gating + sandbox enforcement."""

import os
from pathlib import Path

import pytest

from agentx_dev import Permissions, DefaultTools


class TestPermissionsBasics:

    def test_deny_all_default(self):
        p = Permissions.deny_all()
        assert not p.read_files
        assert not p.write_files
        assert not p.execute_python

    def test_read_only_grants_only_reads(self, tmp_path):
        p = Permissions.read_only(allowed_paths=[str(tmp_path)])
        assert p.read_files
        assert p.list_directories
        assert not p.write_files
        assert not p.edit_files
        assert not p.execute_python

    def test_full_access_grants_everything(self, tmp_path):
        p = Permissions.full_access([str(tmp_path)])
        assert p.read_files
        assert p.write_files
        assert p.edit_files
        assert p.execute_python


class TestCapabilityGating:
    """Denied capabilities must not register their tools."""

    def test_only_granted_tools_registered(self, tmp_workspace):
        p = Permissions(
            read_files=True,
            list_directories=True,
            allowed_paths=[str(tmp_workspace)],
        )
        tools = DefaultTools.build(p)
        names = {t.name for t in tools}
        assert "read_path" in names
        assert "list_directory" in names
        assert "write_file" not in names
        assert "delete_path" not in names
        assert "run_python" not in names

    def test_full_access_registers_all(self, tmp_workspace):
        p = Permissions.full_access([str(tmp_workspace)])
        tools = DefaultTools.build(p)
        names = {t.name for t in tools}
        # Every tool should appear
        for expected in [
            "read_path", "list_directory", "find_files", "grep",
            "write_file", "edit_file",
        ]:
            assert expected in names, f"{expected} missing under full_access"

    def test_deny_all_registers_nothing(self):
        tools = DefaultTools.build(Permissions.deny_all())
        assert tools == []


class TestSandboxEnforcement:

    def test_read_outside_sandbox_refused(self, tmp_workspace, tmp_path):
        # Create a file OUTSIDE the sandbox
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")

        p = Permissions(read_files=True, allowed_paths=[str(tmp_workspace)])
        tools = DefaultTools.build(p)
        read = next(t for t in tools if t.name == "read_path")
        with pytest.raises(PermissionError) as exc_info:
            read.func(path=str(outside))
        assert "outside" in str(exc_info.value).lower() or "sandbox" in str(exc_info.value).lower()

    def test_traversal_attack_refused(self, tmp_workspace):
        p = Permissions(read_files=True, allowed_paths=[str(tmp_workspace)])
        tools = DefaultTools.build(p)
        read = next(t for t in tools if t.name == "read_path")
        with pytest.raises(PermissionError):
            # Attempt ../../etc/passwd style path -- must be refused.
            read.func(path=str(tmp_workspace) + "/../../etc/passwd")

    def test_write_inside_sandbox_allowed(self, tmp_workspace):
        p = Permissions(
            read_files=True, write_files=True,
            allowed_paths=[str(tmp_workspace)],
            workspace=str(tmp_workspace),
        )
        tools = DefaultTools.build(p)
        write = next(t for t in tools if t.name == "write_file")
        result = write.func(path="hello.txt", content="hi")
        assert "hi" == (tmp_workspace / "hello.txt").read_text()


class TestSessionSanitizer:
    """3.0.6 session_id sanitizer -- path separators, .., leading dots refused."""

    def test_bad_session_id_rejected(self, tmp_path):
        from agentx_dev import Permissions
        with pytest.raises((ValueError, Exception)):
            Permissions.new_session(base=str(tmp_path), session_id="../../../tmp/pwn")

    def test_good_session_id_accepted(self, tmp_path):
        p = Permissions.new_session(base=str(tmp_path), session_id="run_abc123")
        assert p is not None
        assert p.workspace
