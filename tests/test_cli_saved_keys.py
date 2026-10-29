"""API keys saved in Windows after the terminal opened are picked up when this run does not have them."""

from agentx_dev.cli import app as app_module
from agentx_dev.cli.models import build_team_models
from tests.conftest import MockModel
from tests.test_cli_app import Lines, make_app

SECRET = "sk-test-not-a-real-key-123"


def test_a_missing_key_is_taken_from_the_saved_variables(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
    app._saved_key_reader = lambda: {"OPENAI_API_KEY": SECRET, "OTHER": "x"}
    assert app.pick_up_saved_keys() == ["OPENAI_API_KEY"]
    assert app.env["OPENAI_API_KEY"] == SECRET and "OTHER" not in app.env
    text = out.getvalue()
    assert "OPENAI_API_KEY" in text and SECRET not in text              # the name is shown, never the value


def test_a_key_this_run_has_wins(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), env={"OPENAI_API_KEY": "from-terminal"})
    app._saved_key_reader = lambda: {"OPENAI_API_KEY": SECRET}
    assert app.pick_up_saved_keys() == [] and app.env["OPENAI_API_KEY"] == "from-terminal"
    assert "saved Windows variables" not in out.getvalue()


def test_an_explicit_env_never_reads_the_registry(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())          # make_app passes env={}
    assert app.pick_up_saved_keys() == [] and app._saved_key_reader() == {}


def test_a_broken_reader_is_ignored(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())

    def broken():
        raise OSError("no registry")
    app._saved_key_reader = broken
    assert app.pick_up_saved_keys() == []


def test_a_team_model_of_the_other_provider_is_used_once_its_key_is_picked_up(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), env={"ANTHROPIC_API_KEY": "a"})
    names = ["gpt-5.5"]
    _, notes = build_team_models(names, app.env, factory=lambda name, env: MockModel())
    assert any("no OPENAI_API_KEY" in note for note in notes)
    app._saved_key_reader = lambda: {"OPENAI_API_KEY": SECRET}
    app.pick_up_saved_keys()
    built, notes = build_team_models(names, app.env, factory=lambda name, env: MockModel())
    assert [m.name for m in built] == names and not notes


def test_saved_keys_keeps_only_api_keys_and_the_user_value_wins(monkeypatch):
    monkeypatch.setattr(app_module, "windows_system_env", lambda: {"OPENAI_API_KEY": "system", "PATH": "x"})
    monkeypatch.setattr(app_module, "windows_user_env", lambda: {"OPENAI_API_KEY": "user"})
    assert app_module.saved_keys() == {"OPENAI_API_KEY": "user"}
