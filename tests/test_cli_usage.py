"""/usage: the tokens this session has used, per model, and what the last turn cost."""

from agentx_dev.cli.commands import command_names, run_command
from tests.conftest import MockModel, make_final
from tests.test_cli_app import Lines, make_app


def run_session(tmp_path, *lines, model=None, **kw):
    model = model or MockModel(script=[make_final("hi")] * 5)
    app, out, cwd, home = make_app(tmp_path, Lines(*lines, "/exit"), model, width=300, **kw)
    assert app.start() == 0
    return app, out, model


def usage_text(app, out):
    out.truncate(0)
    out.seek(0)
    run_command(app, "/usage")
    return out.getvalue()


def test_usage_is_a_listed_command():
    assert "/usage" in command_names()


def test_before_any_model_call_it_says_so(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=300)
    run_command(app, "/usage")
    assert "No model calls yet" in out.getvalue()


def test_after_a_turn_it_shows_calls_input_and_output_tokens_for_the_session_and_the_last_turn(tmp_path):
    app, out, model = run_session(tmp_path, "hello there")
    text = usage_text(app, out)
    calls = model.usage.total_calls
    assert calls >= 1
    assert f"{model.usage.total_input_tokens:,}" in text and f"{model.usage.total_output_tokens:,}" in text
    assert "Session" in text and "Last turn" in text and f"{calls} request" in text


def test_the_last_turn_is_only_that_turn(tmp_path):
    app, out, model = run_session(tmp_path, "one", "two")
    text = usage_text(app, out)
    session_calls = model.usage.total_calls
    turn_calls = int(text.split("Last turn:")[1].split(" request")[0])
    assert session_calls == 2 * turn_calls and turn_calls >= 1


def test_models_the_team_used_are_listed_separately(tmp_path):
    app, out, model = run_session(tmp_path, "hello")
    helper = MockModel()
    helper._record_usage_counts(input_tokens=1234, output_tokens=56)
    app._track_usage_model("gpt-5.5", helper, "helper")
    text = usage_text(app, out)
    assert "gpt-5.5" in text and "helper" in text and "1,234" in text and "56" in text


def test_switching_the_model_keeps_what_the_old_one_used(tmp_path):
    app, out, model = run_session(tmp_path, "hello")
    second = MockModel(script=[make_final("again")])
    second._record_usage_counts(input_tokens=777, output_tokens=7)
    app._track_usage_model("other-model", second, "chat")
    text = usage_text(app, out)
    assert f"{model.usage.total_input_tokens:,}" in text and "777" in text


def test_it_never_raises_when_a_model_has_no_usage(tmp_path):
    app, out, model = run_session(tmp_path, "hello")
    app._track_usage_model("odd", object(), "helper")
    assert "Session" in usage_text(app, out)
