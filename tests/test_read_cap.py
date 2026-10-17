"""``Permissions.read_max_chars``: a big file comes back in pieces, not whole, so one read cannot flood the history."""

from agentx_dev import DefaultTools, Permissions


def tools_for(tmp_path, cap=None):
    perms = Permissions.full_access([str(tmp_path)])
    perms.read_max_chars = cap
    return {tool.name: tool for tool in DefaultTools.build(perms)}


def big_file(tmp_path, lines=400):
    path = tmp_path / "big.txt"
    path.write_text("\n".join(f"line {i:04d} " + "x" * 70 for i in range(1, lines + 1)) + "\n", encoding="utf-8")
    return path


def test_no_cap_is_the_default_and_reads_everything(tmp_path):
    path = big_file(tmp_path)
    text = tools_for(tmp_path)["read_path"].func(path=str(path))
    assert Permissions().read_max_chars is None
    assert text == path.read_text(encoding="utf-8")


def test_a_file_under_the_cap_comes_back_exactly_as_before(tmp_path):
    path = tmp_path / "small.txt"
    path.write_text("hello\nworld\n", encoding="utf-8")
    assert tools_for(tmp_path, cap=10_000)["read_path"].func(path=str(path)) == "hello\nworld\n"


def test_a_big_file_is_cut_with_a_line_numbered_view_and_how_to_continue(tmp_path):
    path = big_file(tmp_path)
    out = tools_for(tmp_path, cap=5_000)["read_path"].func(path=str(path))
    assert out.startswith("1\tline 0001 ")
    assert len(out) < 5_000 + 400                                     # the cap plus the one-line note
    import re
    shown = int(re.search(r"Showing lines 1-(\d+) of 400", out).group(1))
    assert 40 < shown < 400 and f"offset={shown + 1}" in out and "5,000" in out


def test_the_next_piece_starts_where_the_note_says(tmp_path):
    path = big_file(tmp_path)
    read = tools_for(tmp_path, cap=5_000)["read_path"].func
    first = read(path=str(path))
    nxt = int(first.split("offset=")[1].split(",")[0].split(")")[0])
    second = read(path=str(path), offset=nxt, limit=1000)
    assert second.startswith(f"{nxt}\tline {nxt:04d} ")


def test_a_slice_that_fits_has_no_note(tmp_path):
    path = big_file(tmp_path)
    out = tools_for(tmp_path, cap=5_000)["read_path"].func(path=str(path), offset=10, limit=5)
    assert out.splitlines()[0].startswith("10\t") and "Showing" not in out


def test_one_enormous_line_is_cut_too(tmp_path):
    path = tmp_path / "min.js"
    path.write_text("a" * 50_000 + "\nb\n", encoding="utf-8")
    out = tools_for(tmp_path, cap=2_000)["read_path"].func(path=str(path))
    assert len(out) < 2_500 and "cut" in out


def test_a_cut_read_still_unlocks_editing(tmp_path):
    path = big_file(tmp_path)
    tools = tools_for(tmp_path, cap=5_000)
    tools["read_path"].func(path=str(path))
    result = tools["edit_file"].func(path=str(path), find="line 0001 ", replace="LINE 0001 ")
    assert not str(result).startswith("ERROR")
    assert path.read_text(encoding="utf-8").startswith("LINE 0001 ")
