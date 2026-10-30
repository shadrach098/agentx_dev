"""A turn that stops before it finishes (out of credit, any error, Ctrl+C) is kept: the message and what it did go
into the conversation and the work log, so "continue", on the same model or after /model, picks up from there.

Before, such a turn was dropped whole: after "Your credit balance is too low" the user switched to GPT and the new
model did not know what the stopped turn had changed."""

import json

from agentx_dev.cli.app import _out_of_credit
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, all_seen_by, make_app


class CreditError(Exception):
    status_code = 400


CREDIT = CreditError("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
                     "'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to "
                     "upgrade or purchase credits.'}}")


def edit_then_fail(error):
    """A model that writes a file, then fails on its next request."""
    calls = {"n": 0}

    def script(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return make_react_response("write_file", {"path": "src/supabase.ts", "content": "fixed\n",
                                                      "if_exists": "refuse"})
        if calls["n"] == 2:
            raise error
        return make_final("picked up")
    return script


def test_out_of_credit_is_recognised():
    assert _out_of_credit(CREDIT)
    assert _out_of_credit(Exception("Error code: 429 - insufficient_quota"))
    assert _out_of_credit(Exception("You exceeded your current quota, please check your plan"))
    assert not _out_of_credit(RuntimeError("connection reset"))


def test_out_of_credit_says_what_to_do(tmp_path):
    app, out, cwd, _ = make_app(tmp_path, Lines("fix the invite link", "y", "/exit"),
                                MockModel(script=edit_then_fail(CREDIT)), width=300, model_name="claude-opus-5-5")
    app.start()
    text = out.getvalue()
    assert "account is out of credit" in text and "/model <name> and say continue" in text
    assert "Plans & Billing" in text
    assert "BadRequestError: Error code: 400" not in text


def test_the_stopped_turn_is_in_the_conversation(tmp_path):
    app, out, cwd, _ = make_app(tmp_path, Lines("fix the invite link", "y", "/exit"),
                                MockModel(script=edit_then_fail(CREDIT)), width=300)
    app.start()
    history = app.session.history
    assert history[-2] == {"role": "user", "content": "fix the invite link"}
    note = history[-1]["content"]
    assert history[-1]["role"] == "assistant" and "stopped before it finished" in note
    assert "out of credit" in note and "src/supabase.ts" in note


def test_continue_on_another_model_knows_what_was_done(tmp_path):
    model = MockModel(script=edit_then_fail(CREDIT))
    app, out, cwd, _ = make_app(tmp_path, Lines("fix the invite link", "y", "continue", "/exit"), model, width=300)
    app.start()
    seen = all_seen_by(model)
    assert "This turn stopped before it finished" in seen and "src/supabase.ts" in seen
    assert "Your last turn stopped before it finished" in seen            # the carry-on note with "continue"
    assert "picked up" in out.getvalue()


def test_ctrl_c_keeps_it_too(tmp_path):
    app, out, cwd, _ = make_app(tmp_path, Lines("fix it", "y", "/exit"),
                                MockModel(script=edit_then_fail(KeyboardInterrupt())), width=300)
    app.start()
    assert "the user pressed Ctrl+C" in app.session.history[-1]["content"]
    assert "Turn interrupted (Ctrl+C). What it did so far is kept" in out.getvalue()


def test_it_is_saved_with_the_session(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines("fix the invite link", "y", "/exit"),
                                   MockModel(script=edit_then_fail(CREDIT)), width=300)
    app.start()
    saved = json.loads(app.session_path.read_text(encoding="utf-8"))
    assert "stopped before it finished" in json.dumps(saved)


def test_leaving_says_how_to_pick_it_up(tmp_path):
    app, out, cwd, _ = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=[make_final("hi")]), width=300)
    app.start()
    assert "To pick up this conversation later: agentx --continue" in out.getvalue()


def test_nothing_said_on_leaving_an_empty_conversation(tmp_path):
    app, out, cwd, _ = make_app(tmp_path, Lines("/exit"), MockModel(), width=300)
    app.start()
    assert "agentx --continue" not in out.getvalue()


def test_secrets_are_hidden_in_the_note(tmp_path):
    def script(messages):
        raise RuntimeError("boom")
    app, out, cwd, _ = make_app(tmp_path, Lines("use key sk-test-1234567890abcdefghij now", "/exit"),
                                MockModel(script=script), width=300)
    app.start()
    assert "sk-test-1234567890abcdefghij" not in app.session.history[-1]["content"]
