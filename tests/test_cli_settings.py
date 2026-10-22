"""~/.agentx/settings.json and <project>/.agentx/settings.json."""

import json
from pathlib import Path

import pytest

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
    assert names == ["model", "mode", "add_dirs", "allow", "deny", "problems", "project_allow", "model_source",
                     "team_models", "team_models_source", "team_model_uses", "token_limit"]
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


def test_a_project_cannot_turn_on_auto_mode_but_the_user_can(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(project_settings_path(cwd), {"mode": "auto"})
    s = load_settings(cwd, home)
    assert s.mode is None and any("auto mode" in p and "ignored" in p for p in s.problems)
    put(user_settings_path(home), {"mode": "auto"})
    s = load_settings(cwd, home)
    assert s.mode == "auto"


def test_a_project_folder_is_neither_home_nor_a_drive_root(tmp_path):
    from agentx_dev.cli.settings import is_project_folder
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "proj"
    folder.mkdir()
    assert is_project_folder(folder, home) is True
    assert is_project_folder(home, home) is False
    assert is_project_folder(Path(tmp_path.anchor), home) is False


def test_write_project_model_creates_the_file(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    path, message = write_project_model(tmp_path, "gpt-4o-mini")
    assert path == project_settings_path(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "gpt-4o-mini"}
    assert "Created" in message and "gpt-4o-mini" in message


def test_write_project_model_keeps_the_other_keys(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    put(project_settings_path(tmp_path), {"model": "old", "permissions": {"deny": ["run_shell:*sudo*"]}})
    path, message = write_project_model(tmp_path, "new")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"model": "new", "permissions": {"deny": ["run_shell:*sudo*"]}} and "Updated" in message


def test_write_project_model_never_overwrites_a_file_it_cannot_read(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    put(project_settings_path(tmp_path), "{not json")
    path, message = write_project_model(tmp_path, "new")
    assert path is None and "not valid JSON" in message
    assert project_settings_path(tmp_path).read_text(encoding="utf-8") == "{not json"
    put(project_settings_path(tmp_path), "[1, 2]")
    path, message = write_project_model(tmp_path, "new")
    assert path is None and "not a JSON object" in message


def test_write_project_model_reports_an_unwritable_folder_without_raising(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    (tmp_path / ".agentx").write_text("a file, not a folder", encoding="utf-8")
    path, message = write_project_model(tmp_path, "new")
    assert path is None and "Could not write" in message


def test_ensure_project_settings_writes_once_and_never_overwrites(tmp_path):
    from agentx_dev.cli.settings import ensure_project_settings
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "proj"
    folder.mkdir()
    created = ensure_project_settings(folder, home, "gpt-4o")
    assert created == project_settings_path(folder)
    assert json.loads(created.read_text(encoding="utf-8")) == {"model": "gpt-4o"}
    put(created, {"model": "mine"})
    assert ensure_project_settings(folder, home, "gpt-4o") is None
    assert json.loads(created.read_text(encoding="utf-8")) == {"model": "mine"}


def test_ensure_project_settings_skips_home_a_drive_root_and_no_model(tmp_path):
    from agentx_dev.cli.settings import ensure_project_settings
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "proj"
    folder.mkdir()
    assert ensure_project_settings(home, home, "gpt-4o") is None
    assert not (home / ".agentx" / "settings.json").exists()
    assert ensure_project_settings(Path(tmp_path.anchor), home, "gpt-4o") is None
    assert ensure_project_settings(folder, home, None) is None and ensure_project_settings(folder, home, "") is None
    assert not (folder / ".agentx").exists()


def test_the_folder_file_beats_the_user_file_and_loads_without_problems(tmp_path):
    from agentx_dev.cli.settings import ensure_project_settings
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"model": "gpt-4o"})
    ensure_project_settings(folder, home, "claude-sonnet-4-6")
    settings = load_settings(folder, home)
    assert settings.model == "claude-sonnet-4-6" and settings.problems == []
    assert settings.model_source == project_settings_path(folder)


# -- the "(no model)" placeholder ---------------------------------------------------------------------------

def test_the_first_settings_file_never_holds_a_placeholder_model(tmp_path):
    from agentx_dev.cli.settings import ensure_user_settings
    for n, placeholder in enumerate(("(no model)", "MockModel", "", None)):
        home = tmp_path / f"home{n}"
        created = ensure_user_settings(home, placeholder)
        data = json.loads(created.read_text(encoding="utf-8"))
        assert "model" not in data and data["mode"] == "default"


def test_a_folder_settings_file_is_never_created_for_a_placeholder_model(tmp_path):
    from agentx_dev.cli.settings import ensure_project_settings
    home, folder = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    folder.mkdir()
    for placeholder in ("(no model)", "MockModel"):
        assert ensure_project_settings(folder, home, placeholder) is None
    assert not (folder / ".agentx").exists()


def test_an_old_placeholder_model_is_ignored_with_a_note_and_the_file_is_left_alone(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    put(user_settings_path(home), {"model": "(no model)", "mode": "plan"})
    before = user_settings_path(home).read_text(encoding="utf-8")
    s = load_settings(cwd, home)
    assert s.model is None and s.model_source is None and s.mode == "plan"
    assert s.problems == [f'"model": "(no model)" in {user_settings_path(home)} is a placeholder written by an '
                          "earlier build; it was ignored - delete that line."]
    assert user_settings_path(home).read_text(encoding="utf-8") == before


def test_the_model_source_names_the_file_the_model_came_from(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    assert load_settings(cwd, home).model_source is None
    put(user_settings_path(home), {"model": "gpt-4o"})
    assert load_settings(cwd, home).model_source == user_settings_path(home)


# -- the folder settings file is never written through a link ------------------------------------------------

def _outside_settings(tmp_path):
    """A project whose .agentx is a link (a symlink, or a junction on Windows) to another settings folder."""
    elsewhere, project = tmp_path / "elsewhere", tmp_path / "proj"
    elsewhere.mkdir()
    project.mkdir()
    (elsewhere / "settings.json").write_text('{"model": "claude-sonnet-4-6"}', encoding="utf-8")
    _link(project / ".agentx", elsewhere)
    return project, elsewhere


def test_write_project_model_refuses_a_linked_settings_folder(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    project, elsewhere = _outside_settings(tmp_path)
    path, message = write_project_model(project, "gpt-4o")
    assert path is None
    assert message == f"{project_settings_path(project)} is a link or points outside the folder, so it was left unchanged."
    assert (elsewhere / "settings.json").read_text(encoding="utf-8") == '{"model": "claude-sonnet-4-6"}'
    assert sorted(p.name for p in elsewhere.iterdir()) == ["settings.json"]


def test_ensure_project_settings_refuses_a_linked_settings_folder(tmp_path):
    from agentx_dev.cli.settings import ensure_project_settings
    project, elsewhere = _outside_settings(tmp_path)
    (elsewhere / "settings.json").unlink()
    home = tmp_path / "home"
    home.mkdir()
    assert ensure_project_settings(project, home, "gpt-4o") is None
    assert list(elsewhere.iterdir()) == []


def test_a_linked_settings_file_is_not_written_through(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".agentx").mkdir(parents=True)
    try:
        (project / ".agentx" / "settings.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks cannot be created here")
    path, message = write_project_model(project, "gpt-4o")
    assert path is None and "is a link" in message and outside.read_text(encoding="utf-8") == "{}"


def test_a_planted_settings_tmp_link_is_never_followed(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".agentx").mkdir(parents=True)
    try:
        (project / ".agentx" / "settings.tmp").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks cannot be created here")
    path, _ = write_project_model(project, "gpt-4o")
    assert path == project_settings_path(project)
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_the_temporary_file_has_a_fresh_name_so_a_planted_one_is_never_opened(tmp_path):
    """Runs where symlinks cannot be made: a file already at the old fixed temporary name is left alone."""
    from agentx_dev.cli.settings import write_project_model
    put(tmp_path / ".agentx" / "settings.tmp", "planted")
    path, _ = write_project_model(tmp_path, "gpt-4o")
    assert path is not None and (tmp_path / ".agentx" / "settings.tmp").read_text(encoding="utf-8") == "planted"
    assert sorted(p.name for p in (tmp_path / ".agentx").iterdir()) == ["settings.json", "settings.tmp"]


@pytest.mark.skipif(__import__("os").name == "nt", reason="file modes are not meaningful on Windows")
def test_an_updated_folder_settings_file_keeps_its_mode(tmp_path):
    import os
    from agentx_dev.cli.settings import write_project_model
    put(project_settings_path(tmp_path), {"model": "old"})
    os.chmod(project_settings_path(tmp_path), 0o640)
    write_project_model(tmp_path, "gpt-4o")
    assert (project_settings_path(tmp_path).stat().st_mode & 0o777) == 0o640


def test_a_failed_replace_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    from agentx_dev.cli import safepaths
    from agentx_dev.cli.settings import write_project_model
    put(project_settings_path(tmp_path), {"model": "old"})

    def refuse(source, target):
        raise PermissionError("in use")

    monkeypatch.setattr(safepaths.os, "replace", refuse)
    path, message = write_project_model(tmp_path, "new")
    assert path is None and "Could not write" in message
    assert sorted(p.name for p in (tmp_path / ".agentx").iterdir()) == ["settings.json"]
    assert json.loads(project_settings_path(tmp_path).read_text(encoding="utf-8")) == {"model": "old"}


def test_team_models_are_read_and_cleaned(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5", "mistral-x", "gpt-5.5", "claude-opus-5-5"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["claude-opus-5-5", "gpt-5.5"]
    assert settings.team_models_source == user_settings_path(home)
    assert any("mistral-x" in p for p in settings.problems)


def test_no_team_models_key_means_none(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    settings = load_settings(folder, home)
    assert settings.team_models is None and settings.team_models_source is None


def test_the_folder_list_replaces_the_user_list(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5"]})
    put(project_settings_path(folder), {"teamModels": ["gpt-5.5"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["gpt-5.5"] and settings.team_models_source == project_settings_path(folder)


def test_an_empty_folder_list_turns_extra_models_off_for_that_folder(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": ["claude-opus-5-5"]})
    put(project_settings_path(folder), {"teamModels": []})
    assert load_settings(folder, home).team_models == []


def test_team_models_that_are_not_a_list_are_reported_and_ignored(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": "claude-opus-5-5"})
    settings = load_settings(folder, home)
    assert settings.team_models is None and any("'teamModels' must be a list" in p for p in settings.problems)


def test_write_project_team_models_creates_and_keeps_other_keys(tmp_path):
    from agentx_dev.cli.settings import write_project_team_models
    put(project_settings_path(tmp_path), {"model": "claude-sonnet-5-5"})
    path, message = write_project_team_models(tmp_path, ["claude-opus-5-5", "gpt-5.5"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"model": "claude-sonnet-5-5", "teamModels": ["claude-opus-5-5", "gpt-5.5"]}
    assert message.startswith("Updated") and "helper models claude-opus-5-5, gpt-5.5" in message
    path, message = write_project_team_models(tmp_path, [])
    assert json.loads(path.read_text(encoding="utf-8"))["teamModels"] == [] and "no helper models" in message


def test_write_project_team_models_never_overwrites_invalid_json(tmp_path):
    from agentx_dev.cli.settings import write_project_team_models
    put(project_settings_path(tmp_path), "{not json")
    path, message = write_project_team_models(tmp_path, ["gpt-5.5"])
    assert path is None and "not valid JSON" in message
    assert project_settings_path(tmp_path).read_text(encoding="utf-8") == "{not json"


def test_write_project_model_still_says_what_it_did(tmp_path):
    from agentx_dev.cli.settings import write_project_model
    path, message = write_project_model(tmp_path, "gpt-4o-mini")
    assert message == f"Created {path} with model gpt-4o-mini."


def test_team_model_notes_are_read_from_objects_and_name_equals_text(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": [{"model": "gpt-6.1-sol", "use": "hard coding"},
                                                  "gpt-6-luna=fast reading", "gpt-5.5"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["gpt-6.1-sol", "gpt-6-luna", "gpt-5.5"]
    assert settings.team_model_uses == {"gpt-6.1-sol": "hard coding", "gpt-6-luna": "fast reading"}


def test_the_folder_list_replaces_the_user_notes_too(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "proj"
    folder.mkdir()
    put(user_settings_path(home), {"teamModels": [{"model": "gpt-5.5", "use": "x"}]})
    put(project_settings_path(folder), {"teamModels": ["gpt-6-luna"]})
    settings = load_settings(folder, home)
    assert settings.team_models == ["gpt-6-luna"] and settings.team_model_uses == {}


def test_write_project_team_models_saves_notes_as_objects(tmp_path):
    from agentx_dev.cli.settings import write_project_team_models
    path, message = write_project_team_models(tmp_path, ["gpt-6.1-sol", "gpt-5.5"], {"gpt-6.1-sol": "hard coding"})
    assert json.loads(path.read_text(encoding="utf-8"))["teamModels"] == [
        {"model": "gpt-6.1-sol", "use": "hard coding"}, "gpt-5.5"]
    assert "helper models gpt-6.1-sol, gpt-5.5" in message
