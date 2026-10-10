"""What an approval prompt shows."""

from agentx_dev.cli.preview import MAX_COMMAND_CHARS, MAX_PREVIEW_LINES, preview_for


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


# --- fix round 1: never raise, mirror the tools, bound reads ---------------

import os
import sys

import pytest


def test_a_path_with_a_nul_byte_does_not_crash(tmp_path):
    p = preview_for("write_file", {"path": "a\x00b", "content": "x", "if_exists": "refuse"}, tmp_path)
    assert p.kind == "diff" and p.title.startswith("Create")
    assert "file not found" in preview_for("edit_file", {"path": "a\x00b", "find": "a", "replace": "b"}, tmp_path).body


def test_a_tilde_user_path_does_not_crash(tmp_path):
    p = preview_for("write_file", {"path": "~nosuchuser_zz/x", "content": "x"}, tmp_path)
    assert p.kind == "diff"
    assert "file not found" in preview_for("edit_file", {"path": "~nosuchuser_zz/x", "find": "a", "replace": "b"}, tmp_path).body


def test_tilde_is_not_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("SECRET_HOME_CONTENT\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work = tmp_path / "work"
    work.mkdir()
    p = preview_for("write_file", {"path": "~/.bashrc", "content": "mine\n", "if_exists": "overwrite"}, work)
    assert "SECRET_HOME_CONTENT" not in p.body and p.title == "Create ~/.bashrc"
    e = preview_for("edit_file", {"path": "~/.bashrc", "find": "SECRET", "replace": "x"}, work)
    assert "SECRET_HOME_CONTENT" not in e.body and "file not found" in e.body


def test_an_invalid_utf8_file_does_not_crash(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"caf\xe9\nline\n")
    p = preview_for("write_file", {"path": "b.txt", "content": "new\n", "if_exists": "overwrite"}, tmp_path)
    assert p.kind == "diff" and "+new" in p.body
    e = preview_for("edit_file", {"path": "b.txt", "find": "line", "replace": "x"}, tmp_path)
    assert e.kind == "text" and "UTF-8" in e.body


def test_a_binary_file_is_not_diffed(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02" * 10)
    p = preview_for("write_file", {"path": "b.bin", "content": "x", "if_exists": "overwrite"}, tmp_path)
    assert p.kind == "text" and "binary" in p.body


def test_a_file_over_the_cap_is_not_read(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"a\n" * 600_000)
    p = preview_for("write_file", {"path": "big.txt", "content": "x", "if_exists": "overwrite"}, tmp_path)
    assert p.kind == "text" and "too large" in p.body and "1200000" in p.body
    e = preview_for("edit_file", {"path": "big.txt", "find": "a", "replace": "b"}, tmp_path)
    assert "too large" in e.body


def test_huge_new_content_is_not_diffed(tmp_path):
    p = preview_for("write_file", {"path": "n.txt", "content": "x" * 5_000_000}, tmp_path)
    assert p.kind == "text" and "too large" in p.body


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_not_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    p = preview_for("write_file", {"path": "pipe", "content": "x", "if_exists": "overwrite"}, tmp_path)
    assert p.kind == "diff" and "not a regular file" in p.title
    assert "not a regular file" in preview_for("edit_file", {"path": "pipe", "find": "a", "replace": "b"}, tmp_path).body


def test_edit_file_refuses_a_text_that_appears_twice(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    p = preview_for("edit_file", {"path": "a.py", "find": "x = 1", "replace": "x = 2"}, tmp_path)
    assert p.kind == "text" and "2 times" in p.body and "refused" in p.body


def test_edit_file_unescapes_json_escaped_find_like_the_tool(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    bs = chr(92)                      # the model sent a backslash and an n, not a newline
    find, replace = f"a = 1{bs}nb = 2", f"a = 9{bs}nb = 8"
    p = preview_for("edit_file", {"path": "a.py", "find": find, "replace": replace}, tmp_path)
    assert p.kind == "diff" and "-a = 1" in p.body and "+b = 8" in p.body


@pytest.mark.parametrize("args", [["a", "b"], "just a string", None, 5])
def test_non_dict_args_do_not_crash(tmp_path, args):
    for tool in ("write_file", "edit_file", "run_shell", "mystery"):
        p = preview_for(tool, args, tmp_path)
        assert p.kind == "text" and p.title == tool


def test_an_unknown_tool_with_tuple_keys_and_odd_values(tmp_path):
    p = preview_for("mcp_thing", {("a", 1): object(), 2: {3}}, tmp_path)
    assert p.title == "mcp_thing" and p.kind == "text" and p.body


def test_the_unknown_tool_fallback_marks_a_cut(tmp_path):
    p = preview_for("mcp_thing", {"q": "z" * (MAX_COMMAND_CHARS + 5000)}, tmp_path)
    assert p.truncated and "more characters hidden]" in p.body.splitlines()[-1]
    assert len(p.body) < MAX_COMMAND_CHARS + 100


def test_a_shell_command_and_code_are_shown_whole_up_to_the_cap(tmp_path):
    cmd = "\n".join(f"echo {i}" for i in range(MAX_PREVIEW_LINES + 300)) + "\n" + "x" * 2500 + "; rm -rf ~"
    for tool, key in (("run_shell", "command"), ("run_python", "code")):
        p = preview_for(tool, {key: cmd}, tmp_path)
        assert p.body == cmd and not p.truncated


def test_a_shell_command_over_the_cap_is_cut_and_says_how_much(tmp_path):
    for tool, key in (("run_shell", "command"), ("run_python", "code")):
        p = preview_for(tool, {key: "x" * (MAX_COMMAND_CHARS + 80_000)}, tmp_path)
        assert p.truncated and p.body.endswith("... [80000 more characters hidden]")
        assert len(p.body) < MAX_COMMAND_CHARS + 100


def test_a_cut_line_says_how_many_characters_are_hidden(tmp_path):
    p = preview_for("write_file", {"path": "n.txt", "content": "y" * 2499 + "\n", "if_exists": "refuse"}, tmp_path)
    assert "... [500 more characters hidden]" in p.body


def test_a_line_ending_only_change_is_not_called_no_change(tmp_path):
    (tmp_path / "f.txt").write_text("a", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "a\n", "if_exists": "overwrite"}, tmp_path)
    assert "no change" not in p.body and "final newline" in p.body
    (tmp_path / "g.txt").write_bytes(b"a\r\nb\r\n")
    q = preview_for("write_file", {"path": "g.txt", "content": "a\nb\n", "if_exists": "overwrite"}, tmp_path)
    assert "line endings" in q.body
    (tmp_path / "h.txt").write_bytes(b"a\n")
    r = preview_for("write_file", {"path": "h.txt", "content": "a\n", "if_exists": "overwrite"}, tmp_path)
    assert r.body == "(no change)"


def test_if_exists_is_normalised_like_the_tool(tmp_path):
    (tmp_path / "f.txt").write_text("one\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "two\n", "if_exists": " Append "}, tmp_path)
    assert p.title == "Append to f.txt"
    p = preview_for("write_file", {"path": "f.txt", "content": "two\n", "if_exists": None}, tmp_path)
    assert "refused unless" in p.title
    bad = preview_for("write_file", {"path": "f.txt", "content": "two\n", "if_exists": "clobber"}, tmp_path)
    assert bad.kind == "text" and "not valid" in bad.body


def test_refuse_mode_on_an_existing_file_does_not_promise_a_write(tmp_path):
    (tmp_path / "f.txt").write_text("one\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "f.txt", "content": "two\n"}, tmp_path)
    assert p.title.startswith("Write f.txt") and "refused unless it was read" in p.title
    assert "-one" in p.body and "+two" in p.body


def test_rename_mode_names_the_file_the_tool_would_pick(tmp_path):
    (tmp_path / "r.md").write_text("old\n", encoding="utf-8")
    (tmp_path / "r_1.md").write_text("old\n", encoding="utf-8")
    p = preview_for("write_file", {"path": "r.md", "content": "n\n", "if_exists": "rename"}, tmp_path)
    assert "new name" in p.title and "r_2.md" in p.title


def _write(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)


def test_every_preview_that_cannot_show_the_content_is_marked_unavailable(tmp_path):
    from agentx_dev.cli.preview import MAX_READ_BYTES
    _write(tmp_path, "small.txt", b"old\n")
    _write(tmp_path, "big.bin", b"x" * (MAX_READ_BYTES + 1))
    _write(tmp_path, "bin.dat", b"\x00\x01\x02")
    _write(tmp_path, "latin.txt", b"caf\xe9\n")
    cases = {
        "content too large": ("write_file", {"path": "small.txt", "content": "y" * 1_200_000,
                                              "if_exists": "overwrite"}),
        "too many lines": ("write_file", {"path": "small.txt", "content": "y\n" * 25_000,
                                          "if_exists": "overwrite"}),
        "file too large": ("write_file", {"path": "big.bin", "content": "z", "if_exists": "overwrite"}),
        "file is binary": ("write_file", {"path": "bin.dat", "content": "z", "if_exists": "overwrite"}),
        "edit: file too large": ("edit_file", {"path": "big.bin", "find": "x", "replace": "y"}),
        "edit: binary": ("edit_file", {"path": "bin.dat", "find": "x", "replace": "y"}),
        "edit: not utf-8": ("edit_file", {"path": "latin.txt", "find": "caf", "replace": "tea"}),
        "new file too large": ("write_file", {"path": "new.txt", "content": "y" * 1_200_000,
                                              "if_exists": "refuse"}),
    }
    for label, (tool, args) in cases.items():
        p = preview_for(tool, args, tmp_path)
        assert p.truncated, label
        assert p.reason.startswith("preview unavailable (") and p.reason.endswith("); nothing was run"), label


def test_the_counted_200_line_cut_stays_reviewable(tmp_path):
    content = "".join(f"line {i}\n" for i in range(MAX_PREVIEW_LINES + 50))
    p = preview_for("write_file", {"path": "n.txt", "content": content, "if_exists": "refuse"}, tmp_path)
    assert not p.truncated and p.body.endswith("more lines")


def test_ordinary_previews_are_not_marked(tmp_path):
    (tmp_path / "f.txt").write_text("a\n", encoding="utf-8")
    for tool, args in (("write_file", {"path": "f.txt", "content": "b\n", "if_exists": "overwrite"}),
                       ("edit_file", {"path": "f.txt", "find": "a", "replace": "b"}),
                       ("run_shell", {"command": "ls"}), ("delete_path", {"path": "f.txt"})):
        assert not preview_for(tool, args, tmp_path).truncated
