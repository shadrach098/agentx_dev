"""What an approval prompt shows."""

from agentx_dev.cli.preview import MAX_PREVIEW_LINES, preview_for


def test_a_new_file_is_all_additions(tmp_path):
    p = preview_for("write_file", {"path": "new.py", "content": "a\nb\n", "if_exists": "refuse"}, tmp_path)
    assert p.title == "Create new.py" and p.kind == "diff"
    assert "+a" in p.body and "+b" in p.body
    removed = [line for line in p.body.splitlines() if line.startswith("-") and not line.startswith("---")]
    assert removed == []


def test_overwriting_shows_a_diff_against_the_file_on_disk(tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "one\nTWO\n", "if_exists": "overwrite"}, tmp_path)
    assert p.title == "Overwrite f.txt" and "-two" in p.body and "+TWO" in p.body


def test_append_shows_only_the_added_lines(tmp_path):
    (tmp_path / "f.txt").write_text("one\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "two\n", "if_exists": "append"}, tmp_path)
    assert p.title == "Append to f.txt" and "+two" in p.body and "-one" not in p.body


def test_rename_mode_treats_the_content_as_a_new_file(tmp_path):
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "new\n", "if_exists": "rename"}, tmp_path)
    assert "new name" in p.title and "+new" in p.body and "-old" not in p.body


def test_edit_file_shows_the_replacement(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    p = preview_for("edit_file", {"path": "a.py", "find": "y = 2", "replace": "y = 3"}, tmp_path)
    assert p.kind == "diff" and "-y = 2" in p.body and "+y = 3" in p.body


def test_edit_file_with_text_that_is_not_there(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    p = preview_for("edit_file", {"path": "a.py", "find": "zzz", "replace": "q"}, tmp_path)
    assert p.kind == "text" and "not found" in p.body


def test_edit_file_on_a_missing_file(tmp_path):
    p = preview_for("edit_file", {"path": "nope.py", "find": "a", "replace": "b"}, tmp_path)
    assert "file not found" in p.body


def test_commands_and_code(tmp_path):
    shell = preview_for("run_shell", {"command": "pytest -q"}, tmp_path)
    assert (shell.title, shell.body, shell.kind) == ("Run shell command", "pytest -q", "command")
    in_dir = preview_for("run_shell", {"command": "ls", "cwd": "sub"}, tmp_path)
    assert in_dir.title == "Run shell command in sub"
    py = preview_for("run_python", {"code": "print(1)"}, tmp_path)
    assert (py.title, py.body, py.kind) == ("Run Python", "print(1)", "code")


def test_delete_and_move(tmp_path):
    assert preview_for("delete_path", {"path": "d", "recursive": True}, tmp_path).title == \
        "Delete d and everything inside it"
    assert preview_for("delete_path", {"path": "f"}, tmp_path).title == "Delete f"
    assert preview_for("move_file", {"source": "a", "destination": "b"}, tmp_path).title == "Move a to b"


def test_an_unknown_tool_shows_its_arguments(tmp_path):
    p = preview_for("mcp_thing", {"q": 1}, tmp_path)
    assert p.title == "mcp_thing" and '"q": 1' in p.body


def test_a_long_diff_is_cut(tmp_path):
    content = "".join(f"line {i}\n" for i in range(MAX_PREVIEW_LINES + 50))
    p = preview_for("write_file", {"path": "big.txt", "content": content, "if_exists": "refuse"}, tmp_path)
    lines = p.body.splitlines()
    assert len(lines) == MAX_PREVIEW_LINES + 1 and lines[-1].startswith("... ") and lines[-1].endswith("more lines")


def test_an_unreadable_target_does_not_crash(tmp_path):
    (tmp_path / "adir").mkdir()
    p = preview_for("write_file", {"path": "adir", "content": "x", "if_exists": "refuse"}, tmp_path)
    assert p.kind == "diff"          # a directory cannot be read, so it is previewed like a new file
