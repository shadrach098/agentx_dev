"""edit_file changes the text it was asked to change and nothing else, line endings included."""

import pytest

from agentx_dev.DefaultTools import DefaultTools, Permissions


@pytest.fixture
def tools(tmp_path):
    permissions = Permissions(read_files=True, edit_files=True, workspace=str(tmp_path),
                              allowed_paths=[str(tmp_path)])
    return {t.name: t.func for t in DefaultTools.build(permissions)}


def edit(tools, tmp_path, data: bytes, find: str, replace: str):
    target = tmp_path / "f.txt"
    target.write_bytes(data)
    tools["read_path"](path="f.txt")
    answer = tools["edit_file"](path="f.txt", find=find, replace=replace)
    return target.read_bytes(), answer


def test_an_lf_file_stays_lf(tools, tmp_path):
    data, answer = edit(tools, tmp_path, b"one\ntwo\nthree\n", "two", "2")
    assert data == b"one\n2\nthree\n" and answer.startswith("edited")


def test_an_lf_file_keeps_the_new_lines_lf(tools, tmp_path):
    data, _ = edit(tools, tmp_path, b"one\ntwo\nthree\n", "one\ntwo", "a\nb\nc")
    assert data == b"a\nb\nc\nthree\n"


def test_a_crlf_file_stays_crlf_for_a_single_line_edit(tools, tmp_path):
    data, _ = edit(tools, tmp_path, b"one\r\ntwo\r\nthree\r\n", "two", "2")
    assert data == b"one\r\n2\r\nthree\r\n"


def test_a_crlf_file_can_be_edited_across_lines_with_lf_in_find_and_replace(tools, tmp_path):
    data, answer = edit(tools, tmp_path, b"one\r\ntwo\r\nthree\r\n", "one\ntwo", "a\nb\nc")
    assert data == b"a\r\nb\r\nc\r\nthree\r\n" and answer.startswith("edited")


def test_a_crlf_file_accepts_a_find_that_already_has_crlf(tools, tmp_path):
    data, _ = edit(tools, tmp_path, b"one\r\ntwo\r\nthree\r\n", "one\r\ntwo", "x\r\ny")
    assert data == b"x\r\ny\r\nthree\r\n"


def test_a_crlf_file_without_a_final_newline_and_with_a_bom_is_kept_byte_for_byte(tools, tmp_path):
    data, _ = edit(tools, tmp_path, b"\xef\xbb\xbfone\r\ntwo", "two", "2\n3")
    assert data == b"\xef\xbb\xbfone\r\n2\r\n3"


def test_a_mixed_file_changes_only_the_edited_lines(tools, tmp_path):
    original = b"a\r\nb\r\nc\r\nd\ne\n"            # mostly CRLF
    data, _ = edit(tools, tmp_path, original, "b", "B")
    assert data == b"a\r\nB\r\nc\r\nd\ne\n"
    data, _ = edit(tools, tmp_path, original, "d\ne", "D\nE")           # the LF-ended part is matched as stored
    assert data == b"a\r\nb\r\nc\r\nD\nE\n"
    data, _ = edit(tools, tmp_path, original, "c\nd", "C\nD")             # the CRLF form matches here
    assert data == b"a\r\nb\r\nC\r\nD\ne\n"


def test_a_mixed_file_that_is_mostly_lf_keeps_its_lf(tools, tmp_path):
    original = b"a\nb\nc\nd\r\ne\r\n"
    data, _ = edit(tools, tmp_path, original, "a\nb", "A\nB")
    assert data == b"A\nB\nc\nd\r\ne\r\n"


def test_not_found_and_ambiguous_leave_the_file_alone(tools, tmp_path):
    data, answer = edit(tools, tmp_path, b"x\r\nx\r\n", "x", "y")
    assert data == b"x\r\nx\r\n" and "appears 2 times" in answer
    data, answer = edit(tools, tmp_path, b"x\r\ny\r\n", "z", "y")
    assert data == b"x\r\ny\r\n" and "not found" in answer


# -- the approval preview shows exactly what the tool writes ----------------------------------------------------

CASES = [
    (b"one\ntwo\nthree\n", "two", "2"),
    (b"one\r\ntwo\r\nthree\r\n", "two", "2"),
    (b"one\r\ntwo\r\nthree\r\n", "one\ntwo", "a\nb\nc"),
    (b"one\r\ntwo\r\nthree\r\n", "one\r\ntwo", "x\r\ny"),
    (b"a\r\nb\r\nc\r\nd\ne\n", "d\ne", "D\nE"),
    (b"a\r\nb\r\nc\r\nd\ne\n", "c\nd", "C\nD"),
    (b"a\nb\nc\nd\r\ne\r\n", "a\nb", "A\nB"),
    (b"\xef\xbb\xbfone\r\ntwo", "two", "2\n3"),
]


@pytest.mark.parametrize("data, find, replace", CASES)
def test_the_preview_is_the_diff_of_what_the_tool_writes(tools, tmp_path, data, find, replace):
    from agentx_dev.cli.preview import _diff, preview_for
    target = tmp_path / "f.txt"
    target.write_bytes(data)
    preview = preview_for("edit_file", {"path": "f.txt", "find": find, "replace": replace}, tmp_path)
    tools["read_path"](path="f.txt")
    tools["edit_file"](path="f.txt", find=find, replace=replace)
    expected = _diff(data.decode("utf-8"), target.read_bytes().decode("utf-8"), "f.txt")
    assert preview.kind == "diff" and preview.body == expected


def test_the_preview_of_a_crlf_edit_is_the_one_changed_line(tmp_path):
    from agentx_dev.cli.preview import preview_for
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    body = preview_for("edit_file", {"path": "f.txt", "find": "two", "replace": "2"}, tmp_path).body
    changes = [line for line in body.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert changes == ["-two", "+2"] and "\r" not in body


def test_the_preview_says_not_found_when_the_tool_would(tools, tmp_path):
    from agentx_dev.cli.preview import preview_for
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\n")
    assert "not found" in preview_for("edit_file", {"path": "f.txt", "find": "zzz", "replace": "q"}, tmp_path).body
