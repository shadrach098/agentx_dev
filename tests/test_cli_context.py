"""AGENTX.md discovery and the system addendum."""

import pytest

from agentx_dev.cli.context import MAX_CONTEXT_CHARS, build_addendum, find_context_files


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_files_are_found_farthest_first_with_the_user_file_before_them(tmp_path):
    home = tmp_path / "home"
    user = write(home / ".agentx" / "AGENTX.md", "user rules")
    top = write(tmp_path / "work" / "AGENTX.md", "top rules")
    inner = write(tmp_path / "work" / "app" / "AGENTX.md", "app rules")
    cwd = tmp_path / "work" / "app" / "src"
    cwd.mkdir(parents=True)
    ours = [p.resolve() for p in (user, top, inner)]
    found = [p.resolve() for p in find_context_files(cwd, home)]
    assert [p for p in found if p in ours] == ours        # any AGENTX.md above the temp folder is not ours


def test_no_files_means_only_the_base_prompt(tmp_path):
    text = build_addendum(tmp_path / "proj", tmp_path / "home", system="TestOS")
    assert "Working folder:" in text and "TestOS" in text
    assert "Project instructions" not in text


def test_instructions_are_included_nearest_last(tmp_path):
    home = tmp_path / "home"
    write(tmp_path / "work" / "AGENTX.md", "TOP-RULE")
    write(tmp_path / "work" / "app" / "AGENTX.md", "APP-RULE")
    text = build_addendum(tmp_path / "work" / "app", home, system="X")
    assert "Project instructions" in text
    assert text.index("TOP-RULE") < text.index("APP-RULE")


def test_the_cut_keeps_the_nearest_files(tmp_path):
    home = tmp_path / "home"
    write(tmp_path / "work" / "AGENTX.md", "FAR" + "f" * (MAX_CONTEXT_CHARS - 10))
    write(tmp_path / "work" / "app" / "AGENTX.md", "NEAR" + "n" * (MAX_CONTEXT_CHARS - 10))
    text = build_addendum(tmp_path / "work" / "app", home, system="X")
    assert "NEAR" in text and "FAR" not in text


def test_one_huge_file_is_truncated_with_a_note(tmp_path):
    write(tmp_path / "AGENTX.md", "Q" * (MAX_CONTEXT_CHARS * 2))
    text = build_addendum(tmp_path, tmp_path / "home", system="X")
    assert text.count("Q") == MAX_CONTEXT_CHARS and "truncated" in text


def test_an_unreadable_file_is_skipped(tmp_path):
    (tmp_path / "AGENTX.md").mkdir()          # a directory named AGENTX.md cannot be read as a file
    text = build_addendum(tmp_path, tmp_path / "home", system="X")
    assert "Project instructions" not in text


# --- files from an untrusted checkout must never raise or leak ---

def test_a_very_large_file_is_read_in_part_and_truncated(tmp_path):
    write(tmp_path / "AGENTX.md", "Z" * (6 * 1024 * 1024))
    text = build_addendum(tmp_path, tmp_path / "home", system="X")
    assert text.count("Z") == MAX_CONTEXT_CHARS and "truncated" in text


def test_a_binary_file_is_skipped(tmp_path):
    (tmp_path / "AGENTX.md").write_bytes(b"\x00\x01\x02 secret \x00" * 50)
    text = build_addendum(tmp_path, tmp_path / "home", system="X")
    assert "Project instructions" not in text and "\x00" not in text


def test_bad_encoding_does_not_raise(tmp_path):
    (tmp_path / "AGENTX.md").write_bytes(b"RULE-OK \xff\xfe\xfa end")
    text = build_addendum(tmp_path, tmp_path / "home", system="X")
    assert "RULE-OK" in text


def test_a_symlink_pointing_outside_its_folder_is_not_followed(tmp_path):
    secret = write(tmp_path / "elsewhere" / "secret.txt", "TOP-SECRET-CONTENT")
    project = tmp_path / "proj"
    project.mkdir()
    try:
        (project / "AGENTX.md").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    found = [p.resolve() for p in find_context_files(project, tmp_path / "home")]
    assert secret.resolve() not in found
    assert "TOP-SECRET-CONTENT" not in build_addendum(project, tmp_path / "home", system="X")


def test_a_symlink_inside_its_folder_is_followed(tmp_path):
    project = tmp_path / "proj"
    write(project / "docs" / "rules.md", "INSIDE-RULE")
    try:
        (project / "AGENTX.md").symlink_to(project / "docs" / "rules.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert "INSIDE-RULE" in build_addendum(project, tmp_path / "home", system="X")


def test_a_control_character_in_a_folder_name_is_cleaned_in_the_heading(tmp_path):
    folder = tmp_path / "a\x1bb"
    try:
        folder.mkdir()
    except OSError:
        pytest.skip("folder names with control characters are not available here")
    write(folder / "AGENTX.md", "RULE")
    text = build_addendum(folder, tmp_path / "home", system="X")
    assert "RULE" in text
    assert "\x1b" not in text.split("Project instructions", 1)[1]


def test_the_symlink_rule_without_real_symlinks(tmp_path, monkeypatch):
    """Same rule as above, with is_symlink/resolve faked so it also runs where symlinks cannot be created."""
    from pathlib import Path
    project = tmp_path / "proj"
    write(project / "AGENTX.md", "LEAKED-TEXT")
    real_resolve = Path.resolve
    outside = tmp_path / "elsewhere" / "secret.txt"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "AGENTX.md")
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: outside if self.name == "AGENTX.md" else real_resolve(self, *a, **k))
    assert "LEAKED-TEXT" not in build_addendum(project, tmp_path / "home", system="X")
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: project / "docs" / "r.md" if self.name == "AGENTX.md" else real_resolve(self, *a, **k))
    assert "LEAKED-TEXT" in build_addendum(project, tmp_path / "home", system="X")
