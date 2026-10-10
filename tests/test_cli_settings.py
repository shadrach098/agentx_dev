"""~/.agentx/settings.json and <project>/.agentx/settings.json."""

import json
from pathlib import Path

from agentx_dev.cli.settings import load_settings, project_settings_path, user_settings_path


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_no_files_gives_empty_settings(tmp_path):
    s = load_settings(tmp_path / "proj", tmp_path / "home")
    assert (s.model, s.mode, s.add_dirs, s.allow, s.deny, s.problems) == (None, None, [], [], [], [])


def test_user_and_project_merge(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"model": "gpt-x", "mode": "plan", "addDirs": ["../a"],
                                   "permissions": {"allow": ["run_shell:ls*"], "deny": ["run_shell:*sudo*"]}})
    put(project_settings_path(cwd), {"model": "claude-y", "addDirs": ["../b"],
                                     "permissions": {"allow": ["run_shell:pytest*"], "deny": ["write_file:.env"]}})
    s = load_settings(cwd, home)
    assert s.model == "claude-y" and s.mode == "plan"                       # project over user, scalars
    assert s.add_dirs == [str((tmp_path / "a").resolve()), str((tmp_path / "b").resolve())]   # concatenated, absolute
    assert s.allow == ["run_shell:ls*", "run_shell:pytest*"]
    assert s.deny == ["run_shell:*sudo*", "write_file:.env"]
    assert s.problems == []


def test_invalid_json_is_reported_by_path_and_ignored(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(project_settings_path(cwd), "{not json")
    put(user_settings_path(home), {"model": "gpt-x"})
    s = load_settings(cwd, home)
    assert s.model == "gpt-x"
    assert len(s.problems) == 1 and str(project_settings_path(cwd)) in s.problems[0]


def test_wrong_types_are_reported_and_skipped(tmp_path):
    cwd = tmp_path / "proj"
    put(project_settings_path(cwd), {"model": 7, "mode": "yolo", "addDirs": "../a",
                                     "permissions": {"allow": ["ok", 3, ""], "deny": "x"}})
    s = load_settings(cwd, tmp_path / "home")
    assert (s.model, s.mode, s.add_dirs) == (None, None, [])
    assert s.allow == ["ok"] and s.deny == []
    assert len(s.problems) == 5


def test_a_top_level_list_is_not_settings(tmp_path):
    cwd = tmp_path / "proj"
    put(project_settings_path(cwd), "[1, 2]")
    s = load_settings(cwd, tmp_path / "home")
    assert s.model is None and "expected a JSON object" in s.problems[0]


def test_absurdly_nested_json_is_reported_not_raised(tmp_path):
    cwd = tmp_path / "proj"
    put(project_settings_path(cwd), "[" * 100000)
    s = load_settings(cwd, tmp_path / "home")
    assert s.model is None and len(s.problems) == 1


def test_a_settings_path_that_is_a_directory_is_ignored(tmp_path):
    cwd = tmp_path / "proj"
    project_settings_path(cwd).mkdir(parents=True)
    assert load_settings(cwd, tmp_path / "home").problems == []


def test_filesystem_errors_become_warnings(tmp_path, monkeypatch):
    from pathlib import Path

    def deny(self, *a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "is_file", deny)
    s = load_settings(tmp_path / "proj", tmp_path / "home")
    assert s.model is None and len(s.problems) == 2 and "denied" in s.problems[0]


def test_an_unreadable_file_becomes_a_warning(tmp_path, monkeypatch):
    from pathlib import Path
    cwd = tmp_path / "proj"
    put(project_settings_path(cwd), {"model": "x"})

    def deny(self, *a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "read_text", deny)
    s = load_settings(cwd, tmp_path / "home")
    assert s.model is None and len(s.problems) == 1


def test_untrusted_text_is_quoted_in_warnings(tmp_path):
    cwd = tmp_path / "proj"
    put(cwd / ".agentx" / "settings.json", "{not json \x1b[2J")
    s = load_settings(cwd, tmp_path / "home")
    assert s.problems and all("\x1b" not in p for p in s.problems)


def test_add_dirs_are_absolute_and_resolved_per_file(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"addDirs": ["~/notes", "shared", str(tmp_path / "abs")]})
    put(project_settings_path(cwd), {"addDirs": ["../b", "sub/dir", "~"]})
    s = load_settings(cwd, home)
    expect = [
        (home / "notes").resolve(), (home / "shared").resolve(), (tmp_path / "abs").resolve(),
        (tmp_path / "b").resolve(), (cwd / "sub" / "dir").resolve(), home.resolve(),
    ]
    assert [Path(p) for p in s.add_dirs] == expect
    assert all(Path(p).is_absolute() for p in s.add_dirs)


def test_project_allow_tracks_only_the_project_rules(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"permissions": {"allow": ["a:1"]}})
    put(project_settings_path(cwd), {"permissions": {"allow": ["b:2", "a:1"]}})
    s = load_settings(cwd, home)
    assert s.allow == ["a:1", "b:2", "a:1"]            # merged order and duplicates unchanged
    assert s.project_allow == ["b:2", "a:1"]
    assert load_settings(tmp_path / "none", home).project_allow == []


def test_project_allow_when_project_and_home_are_the_same_folder(tmp_path):
    put(user_settings_path(tmp_path), {"permissions": {"allow": ["u:1"]}})
    s = load_settings(tmp_path, tmp_path)
    assert s.allow == ["u:1", "u:1"] and s.project_allow == ["u:1"]
