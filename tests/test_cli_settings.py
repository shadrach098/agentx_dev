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
    put(project_settings_path(cwd), {"model": "claude-y", "addDirs": ["b"],
                                     "permissions": {"allow": ["run_shell:pytest*"], "deny": ["write_file:.env"]}})
    s = load_settings(cwd, home)
    assert s.model == "claude-y" and s.mode == "plan"                       # project over user, scalars
    assert s.add_dirs == [str((tmp_path / "a").resolve()), str((cwd / "b").resolve())]   # concatenated, absolute
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
    put(project_settings_path(cwd), {"addDirs": ["sub/dir", "./x/../y"]})
    s = load_settings(cwd, home)
    expect = [
        (home / "notes").resolve(), (home / "shared").resolve(), (tmp_path / "abs").resolve(),
        (cwd / "sub" / "dir").resolve(), (cwd / "y").resolve(),
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


def _link(link, target):
    import os
    import subprocess
    import sys
    import pytest
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        if sys.platform != "win32":
            pytest.skip("cannot create a symlink")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if made.returncode != 0:
            pytest.skip("cannot create a symlink or junction")


def test_project_add_dirs_outside_the_project_are_dropped_with_a_warning(tmp_path):
    cwd, outside = tmp_path / "proj", tmp_path / "elsewhere"
    (cwd / "sub").mkdir(parents=True)
    outside.mkdir()
    put(project_settings_path(cwd), {"addDirs": ["/", "~", "../../..", "../elsewhere", "sub", "."]})
    s = load_settings(cwd, tmp_path / "home")
    assert s.add_dirs == [str((cwd / "sub").resolve()), str(cwd.resolve())]
    assert len(s.problems) == 4
    assert all("outside the project; ignored" in p and "--add-dir" in p for p in s.problems)
    assert repr(str(outside.resolve())) in s.problems[3]


def test_project_add_dir_symlink_to_an_outside_directory_is_dropped(tmp_path):
    cwd, outside = tmp_path / "proj", tmp_path / "elsewhere"
    cwd.mkdir()
    outside.mkdir()
    _link(cwd / "link", outside)
    put(project_settings_path(cwd), {"addDirs": ["link"]})
    s = load_settings(cwd, tmp_path / "home")
    assert s.add_dirs == [] and len(s.problems) == 1
    assert repr(str(outside.resolve())) in s.problems[0]


def test_user_add_dirs_outside_the_project_are_kept(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    cwd.mkdir()
    put(user_settings_path(home), {"addDirs": ["/", "../elsewhere", "~/notes"]})
    s = load_settings(cwd, home)
    assert len(s.add_dirs) == 3 and s.problems == []


def test_settings_field_order_ends_with_problems_then_project_allow():
    import dataclasses
    from agentx_dev.cli.settings import Settings
    names = [f.name for f in dataclasses.fields(Settings)]
    assert names == ["model", "mode", "add_dirs", "allow", "deny", "problems", "project_allow"]
    assert Settings(None, None, [], [], [], ["p"]).project_allow == []


def test_a_project_cannot_loosen_the_mode_to_accept_edits(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(project_settings_path(cwd), {"mode": "accept-edits"})
    s = load_settings(cwd, home)
    assert s.mode is None
    assert s.problems == ["project settings asked for accept-edits mode; ignored "
                          "(set it in your own settings or with --mode)"]


def test_a_project_cannot_override_the_users_mode_with_accept_edits(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"mode": "plan"})
    put(project_settings_path(cwd), {"mode": "accept-edits"})
    assert load_settings(cwd, home).mode == "plan"


def test_a_project_may_tighten_or_keep_the_mode(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"mode": "accept-edits"})
    put(project_settings_path(cwd), {"mode": "plan"})
    s = load_settings(cwd, home)
    assert s.mode == "plan" and s.problems == []
    put(project_settings_path(cwd), {"mode": "default"})
    assert load_settings(cwd, home).mode == "default"


def test_the_users_own_accept_edits_is_honoured(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"mode": "accept-edits"})
    s = load_settings(cwd, home)
    assert s.mode == "accept-edits" and s.problems == []


def test_a_first_settings_file_is_written_with_the_model_in_use(tmp_path):
    from agentx_dev.cli.settings import ensure_user_settings
    home = tmp_path / "home"
    created = ensure_user_settings(home, "gpt-4o")
    assert created == user_settings_path(home)
    data = json.loads(created.read_text(encoding="utf-8"))
    assert data == {"model": "gpt-4o", "mode": "default", "addDirs": [],
                    "permissions": {"allow": [], "deny": []}}
    s = load_settings(tmp_path / "proj", home)                 # and the file it writes loads without problems
    assert (s.model, s.mode, s.problems) == ("gpt-4o", "default", [])


def test_an_existing_settings_file_is_never_overwritten(tmp_path):
    from agentx_dev.cli.settings import ensure_user_settings
    home = tmp_path / "home"
    put(user_settings_path(home), {"model": "mine"})
    assert ensure_user_settings(home, "gpt-4o") is None
    assert json.loads(user_settings_path(home).read_text(encoding="utf-8")) == {"model": "mine"}


def test_writing_the_settings_file_never_raises(tmp_path):
    from agentx_dev.cli.settings import ensure_user_settings
    blocker = tmp_path / "home"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    assert ensure_user_settings(blocker, "gpt-4o") is None
