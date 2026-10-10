"""~/.agentx/settings.json and <project>/.agentx/settings.json."""

import json

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
    assert s.add_dirs == ["../a", "../b"]                                  # lists are concatenated
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
