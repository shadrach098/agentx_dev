"""Where sessions live, how they are listed and found."""

import json
from datetime import datetime, timezone

from agentx_dev.cli import sessions as sessions_module
from agentx_dev.cli.sessions import (
    find_session, latest_session, list_sessions, new_session_path, project_slug, sessions_dir,
)


def make(home, cwd, session_id, updated, first_message="hello there"):
    path = new_session_path(home, cwd, session_id, now=datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": session_id, "updated_at": updated,
        "history": [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": first_message}],
    }), encoding="utf-8")
    return path


def test_the_slug_has_no_separators(tmp_path):
    slug = project_slug(tmp_path / "my proj")
    assert "/" not in slug and "\\" not in slug and ":" not in slug and slug.endswith("my proj")


def test_the_path_pattern(tmp_path):
    p = new_session_path(tmp_path / "home", tmp_path / "proj", "abcdef12-3456",
                         now=datetime(2026, 10, 10, 1, 2, 3, tzinfo=timezone.utc))
    assert p.parent == sessions_dir(tmp_path / "home", tmp_path / "proj")
    assert p.name == "20261010T010203Z-abcdef12.json"


def test_sessions_are_listed_newest_first_with_a_title(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "aaaa1111-x", "2026-10-10T10:00:00+00:00", "first conversation")
    make(home, cwd, "bbbb2222-x", "2026-10-10T11:00:00+00:00", "  second\n conversation  ")
    infos = list_sessions(home, cwd)
    assert [i.session_id for i in infos] == ["bbbb2222-x", "aaaa1111-x"]
    assert infos[0].title == "second conversation"            # system message skipped, whitespace collapsed
    assert latest_session(home, cwd).session_id == "bbbb2222-x"


def test_a_long_first_message_is_cut_for_the_title(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "cccc3333-x", "2026-10-10T10:00:00+00:00", "w" * 200)
    assert len(list_sessions(home, cwd)[0].title) == 60


def test_corrupt_files_are_skipped(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "dddd4444-x", "2026-10-10T10:00:00+00:00")
    bad = sessions_dir(home, cwd) / "broken.json"
    bad.write_text("{nope", encoding="utf-8")
    (sessions_dir(home, cwd) / "list.json").write_text("[1]", encoding="utf-8")
    assert [i.session_id for i in list_sessions(home, cwd)] == ["dddd4444-x"]


def test_nothing_saved_yet(tmp_path):
    assert list_sessions(tmp_path / "home", tmp_path / "proj") == []
    assert latest_session(tmp_path / "home", tmp_path / "proj") is None


def test_find_by_id_prefix_or_file_name(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    path = make(home, cwd, "eeee5555-aaaa", "2026-10-10T10:00:00+00:00")
    make(home, cwd, "eeee6666-bbbb", "2026-10-10T11:00:00+00:00")
    assert find_session(home, cwd, "eeee5555").path == path
    assert find_session(home, cwd, path.name).path == path
    assert find_session(home, cwd, "eeee") is None              # ambiguous prefix
    assert find_session(home, cwd, "ee") is None                # too short
    assert find_session(home, cwd, "zzzz9999") is None


# --- hardening: odd files and odd ids must never raise or escape the folder ---

def test_odd_files_are_skipped_without_raising(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "ffff7777-x", "2026-10-10T10:00:00+00:00")
    folder = sessions_dir(home, cwd)
    (folder / "binary.json").write_bytes(b"\xff\xfe\x00\x80 not json")
    (folder / "deep.json").write_text("[" * 100000, encoding="utf-8")
    (folder / "dir.json").mkdir()
    assert [i.session_id for i in list_sessions(home, cwd)] == ["ffff7777-x"]


def test_an_oversized_file_is_skipped(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "gggg8888-x", "2026-10-10T10:00:00+00:00")
    monkeypatch.setattr(sessions_module, "MAX_SESSION_BYTES", 10)
    assert list_sessions(home, cwd) == []


def test_fields_of_the_wrong_type_do_not_raise(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    folder = sessions_dir(home, cwd)
    folder.mkdir(parents=True)
    (folder / "odd.json").write_text(json.dumps({"session_id": 5, "updated_at": None, "history": "text"}), encoding="utf-8")
    (folder / "odd2.json").write_text(json.dumps({"history": [None, 3, {"role": "user", "content": ["x"]}]}), encoding="utf-8")
    infos = list_sessions(home, cwd)
    assert len(infos) == 2 and all(i.title == "(no messages yet)" for i in infos)


def test_control_characters_are_removed_from_the_title(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "hhhh9999-x", "2026-10-10T10:00:00+00:00", "hi \x1b[31mred\x07 there")
    title = list_sessions(home, cwd)[0].title
    assert "\x1b" not in title and "\x07" not in title and "red" in title


def test_a_session_id_cannot_climb_out_of_the_folder(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    p = new_session_path(home, cwd, "../../evil/x", now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert p.parent == sessions_dir(home, cwd)
    assert new_session_path(home, cwd, "").parent == sessions_dir(home, cwd)


def test_a_very_long_path_still_gives_a_short_slug(tmp_path):
    slug = project_slug(tmp_path / ("d" * 400) / "my proj")
    assert len(slug) <= 150 and slug.endswith("my proj")
    assert project_slug(tmp_path / ("d" * 400) / "my proj") == slug
    assert project_slug(tmp_path / ("e" * 400) / "my proj") != slug


def test_a_name_with_path_characters_matches_nothing(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    make(home, cwd, "iiii1010-x", "2026-10-10T10:00:00+00:00")
    assert find_session(home, cwd, "../../etc/passwd") is None
    assert find_session(home, cwd, "") is None
