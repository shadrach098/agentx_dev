"""The first-run question for a folder, and the file that remembers the answers."""

import io
import json
from pathlib import Path

from rich.console import Console

from agentx_dev.cli.trust import (CANCELLED, ensure_trusted, is_trusted, remember_trust, trust_file,
                                  trusted_folders)


def console():
    out = io.StringIO()
    return Console(file=out, width=100, force_terminal=False, color_system=None), out


def test_a_new_folder_is_not_trusted(tmp_path):
    assert is_trusted(tmp_path / "proj", tmp_path / "home") is False


def test_saying_yes_is_remembered_and_the_question_is_not_asked_again(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    asked = []
    c, out = console()
    assert ensure_trusted(proj, home, c, select=lambda opts, d=0, h=None: (asked.append(opts), 0)[1]) is True
    assert "Accessing workspace" in out.getvalue() and str(proj) in out.getvalue()
    assert asked[0] == ["Yes, I trust this folder", "No, exit"]
    assert is_trusted(proj, home)
    c2, _ = console()
    assert ensure_trusted(proj, home, c2, select=lambda *a: 1 / 0) is True          # no question this time
    assert json.loads(trust_file(home).read_text(encoding="utf-8"))["folders"]


def test_a_subfolder_of_a_trusted_project_is_trusted_but_a_sibling_is_not(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    (proj / "sub" / "deeper").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    remember_trust(proj, home)
    assert is_trusted(proj / "sub" / "deeper", home) is True
    assert is_trusted(tmp_path / "other", home) is False


def test_trusting_the_home_folder_does_not_trust_everything_below_it(tmp_path):
    home = tmp_path / "home"
    (home / "code" / "project").mkdir(parents=True)
    remember_trust(home, home)
    assert is_trusted(home, home) is True
    assert is_trusted(home / "code" / "project", home) is False


def test_no_exit_and_every_unclear_answer_is_a_no(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    for answer in (1, CANCELLED):
        c, _ = console()
        assert ensure_trusted(proj, home, c, select=lambda *a, answer=answer: answer) is False
    for typed in ("", "n", "maybe", "no"):
        c, _ = console()
        assert ensure_trusted(proj, home, c, select=None, read_line=lambda prompt, t=typed: t) is False
    c, _ = console()
    assert ensure_trusted(proj, home, c, select=None, read_line=lambda prompt: (_ for _ in ()).throw(EOFError())) is False
    c, _ = console()
    assert ensure_trusted(proj, home, c, select=lambda *a: 1 / 0) is False
    assert not is_trusted(proj, home)


def test_the_typed_fallback_accepts_yes(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    c, _ = console()
    assert ensure_trusted(proj, home, c, select=lambda *a: None, read_line=lambda prompt: "y") is True
    assert is_trusted(proj, home)


def test_a_damaged_trust_file_means_nothing_is_trusted_and_does_not_raise(tmp_path):
    home = tmp_path / "home"
    trust_file(home).parent.mkdir(parents=True)
    for content in ("{not json", "[]", '{"folders": "x"}', '{"folders": [1, null]}', "\x00"):
        trust_file(home).write_text(content, encoding="utf-8")
        assert trusted_folders(home) == [] and is_trusted(tmp_path, home) is False


def test_the_home_folder_gets_a_stronger_warning(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c, out = console()
    ensure_trusted(home, home, c, select=lambda *a: 1)
    assert "home folder" in out.getvalue()


def test_text_in_the_path_cannot_carry_escape_codes(tmp_path):
    proj = tmp_path / "a\x1b[2Jb"
    c, out = console()
    ensure_trusted(proj, tmp_path / "home", c, select=lambda *a: 1)
    assert "\x1b" not in out.getvalue()


def test_saving_failure_is_not_fatal(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    blocker = tmp_path / "home"
    blocker.write_text("a file", encoding="utf-8")
    c, out = console()
    assert ensure_trusted(proj, blocker, c, select=lambda *a: 0) is True
    assert "Could not save" in out.getvalue()
