"""Project memory through the whole app: the work log, starting from it, summarising, the folder settings."""

import json
from pathlib import Path

from agentx_dev.cli import app as app_module
from agentx_dev.cli.compact import SUMMARY_PROMPT_MARK, SUMMARY_TAG
from agentx_dev.cli.sessions import list_sessions
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, make_app, seen_by, write_call

SECRET = "sk-secret-0123456789abcdef"


def log_text(cwd):
    path = cwd / ".agentx" / "log.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_finished_turn_is_written_to_the_log(tmp_path):
    model = MockModel(script=[write_call("hello.txt", "hi\n"), make_final("Created hello.txt.")])
    app, out, cwd, _ = make_app(tmp_path, Lines("create hello.txt", "/exit"), model, read_short=Lines("y"))
    assert app.start() == 0
    text = log_text(cwd)
    assert text.startswith("## ") and " - create hello.txt" in text
    assert "Changed: hello.txt" in text and "Answer: Created hello.txt." in text


def test_a_declined_write_is_not_listed_as_changed(tmp_path):
    model = MockModel(script=[write_call("hello.txt", "hi\n"), make_final("Not written.")])
    app, _, cwd, _ = make_app(tmp_path, Lines("create hello.txt", "/exit"), model, read_short=Lines("n", ""))
    assert app.start() == 0
    assert "Changed:" not in log_text(cwd) and "Answer: Not written." in log_text(cwd)


def test_secrets_never_reach_the_log(tmp_path):
    model = MockModel(script=[make_final(f"I saw {SECRET} in your message")])
    app, _, cwd, _ = make_app(tmp_path, Lines(f"my key is {SECRET}", "/exit"), model,
                              env={"OPENAI_API_KEY": SECRET})
    assert app.start() == 0
    text = log_text(cwd)
    assert text and SECRET not in text and "[hidden]" in text


def test_an_environment_key_without_a_key_shape_is_hidden_too(tmp_path):
    plain_key = "plainvalue0123456789"                     # no pattern matches it: only the environment says it is a key
    model = MockModel(script=[make_final(f"echo {plain_key}")])
    app, _, cwd, _ = make_app(tmp_path, Lines(f"use {plain_key}", "/exit"), model,
                              env={"ANTHROPIC_API_KEY": plain_key})
    assert app.start() == 0
    text = log_text(cwd)
    assert text and plain_key not in text and "[hidden]" in text


def test_an_interrupted_turn_writes_nothing(tmp_path):
    def script(messages):
        raise KeyboardInterrupt

    app, _, cwd, _ = make_app(tmp_path, Lines("first", "/exit"), MockModel(script=script))
    assert app.start() == 0
    assert log_text(cwd) == ""


def test_a_failed_turn_writes_nothing(tmp_path):
    def script(messages):
        raise RuntimeError("provider down")

    app, _, cwd, _ = make_app(tmp_path, Lines("first", "/exit"), MockModel(script=script))
    assert app.start() == 0
    assert log_text(cwd) == ""


def test_no_log_is_kept_for_the_home_folder_or_a_drive_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "is_project_folder", lambda cwd, home: False)
    model = MockModel(script=[make_final("ok")])
    app, _, cwd, _ = make_app(tmp_path, Lines("hello", "/exit"), model, newly_trusted=True)
    assert app.start() == 0
    assert not (cwd / ".agentx").exists()


def test_model_folder_refuses_for_the_home_folder_or_a_drive_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "is_project_folder", lambda cwd, home: False)
    app, out, cwd, _ = make_app(tmp_path, Lines("/model gpt-4o-mini --folder", "/exit"), MockModel(script=[]))
    assert app.start() == 0
    assert not (cwd / ".agentx").exists() and "home folder or a drive root" in out.getvalue()


def test_a_log_that_cannot_be_written_is_noticed_once_and_the_chat_goes_on(tmp_path):
    model = MockModel(script=[make_final("one"), make_final("two")])
    app, out, cwd, _ = make_app(tmp_path, Lines("first", "second", "/exit"), model)
    (cwd / ".agentx").write_text("a file, not a folder", encoding="utf-8")
    assert app.start() == 0
    text = out.getvalue()
    assert text.count("Could not write the work log") == 1 and "two" in text


def test_a_fresh_conversation_starts_from_the_log(tmp_path):
    model = MockModel(script=[make_final("ok")])
    app, _, cwd, _ = make_app(tmp_path, Lines("hello", "/exit"), model)
    (cwd / ".agentx").mkdir()
    (cwd / ".agentx" / "log.md").write_text("## 2026-10-11 10:00 - Earlier job\nChanged: old.py\n\n", encoding="utf-8")
    assert app.start() == 0
    sent = seen_by(model)
    assert "<project-log>" in sent and "Earlier job" in sent and "old.py" in sent


def test_clear_starts_from_the_log_but_resume_and_continue_do_not(tmp_path):
    model = MockModel(script=[make_final("first answer"), make_final("second"), make_final("third")])
    app1, _, cwd, home = make_app(tmp_path, Lines("first topic", "/exit"), model)
    app1.start()                                              # writes a log entry and saves a conversation
    assert "first topic" in log_text(cwd)
    resumed = MockModel(script=[make_final("after continue")])
    app2, _, _, _ = make_app(tmp_path, Lines("go on", "/exit"), resumed)
    app2.start("latest")                                      # --continue
    assert "<project-log>" not in seen_by(resumed)
    cleared = MockModel(script=[make_final("after clear")])
    app3, _, _, _ = make_app(tmp_path, Lines("/clear", "new topic", "/exit"), cleared)
    app3.start()
    assert "<project-log>" in seen_by(cleared) and "first topic" in seen_by(cleared)


def test_resume_of_a_saved_conversation_does_not_add_the_log(tmp_path):
    model = MockModel(script=[make_final("one")])
    app1, _, cwd, home = make_app(tmp_path, Lines("topic one", "/exit"), model)
    app1.start()
    ident = list_sessions(home, cwd)[0].session_id[:8]
    other = MockModel(script=[make_final("two")])
    app2, _, _, _ = make_app(tmp_path, Lines(f"/resume {ident}", "carry on", "/exit"), other)
    app2.start()
    assert "<project-log>" not in seen_by(other)


# -- summarising -------------------------------------------------------------------------------------------

def fat_history(turns=12, size=20_000):
    out = []
    for i in range(turns):
        out += [{"role": "user", "content": f"question {i} " + "x" * size}, {"role": "assistant", "content": f"answer {i}"}]
    return out


class Primed(Lines):
    """read_line that first fills the session history, then answers like Lines."""

    def __init__(self, holder, *items):
        super().__init__(*items)
        self.holder = holder
        self.primed = False

    def __call__(self, prompt=""):
        if not self.primed and self.holder:
            self.primed = True
            self.holder[0].session.history = fat_history()
        return super().__call__(prompt)


def summarising_model(summary="Goal: ask many questions about x. Decided: nothing. Open: all of it."):
    calls = {"summary": 0, "chat": 0}

    def script(messages):
        if SUMMARY_PROMPT_MARK in str(messages[-1]["content"]):
            calls["summary"] += 1
            return summary
        calls["chat"] += 1
        return make_final("ok")

    model = MockModel(script=script)
    model.calls_made = calls
    return model


def test_a_long_conversation_is_summarised_before_the_next_turn(tmp_path):
    model = summarising_model()
    holder = []
    app, out, cwd, _ = make_app(tmp_path, Primed(holder, "go on", "/exit"), model)
    holder.append(app)
    assert app.start() == 0
    history = app.session.history
    assert model.calls_made["summary"] == 1 and model.calls_made["chat"] == 1
    assert any(str(m.get("content", "")).startswith(SUMMARY_TAG) for m in history)
    users = [m for m in history if m["role"] == "user" and str(m["content"]).startswith("question ")]
    assert len(users) == 6 and users[-1]["content"].startswith("question 11 ")
    assert "Summarised" in out.getvalue() and "Earlier conversation summarised" in log_text(cwd)
    # What the model was actually sent for the turn is the summarised history, not the old one.
    chat = [call for call in model.calls if SUMMARY_PROMPT_MARK not in str(call[-1]["content"])]
    sent = " ".join(str(message.get("content")) for message in chat[0])
    assert SUMMARY_TAG in sent and "question 0 " not in sent and "question 11 " in sent and "go on" in sent


def test_a_failed_summary_changes_nothing_and_is_not_retried_every_turn(tmp_path):
    model = summarising_model(summary="")
    holder = []
    app, out, _, _ = make_app(tmp_path, Primed(holder, "one", "two", "three", "/exit"), model)
    holder.append(app)
    assert app.start() == 0
    assert model.calls_made["summary"] == 1                       # tried once, then waits for 10 more messages
    assert len([m for m in app.session.history if str(m.get("content", "")).startswith("question ")]) == 12
    assert out.getvalue().count("could not be summarised") == 1


def test_a_summarised_conversation_resumes_with_a_valid_history(tmp_path):
    model = summarising_model()
    holder = []
    app, _, cwd, home = make_app(tmp_path, Primed(holder, "go on", "/exit"), model)
    holder.append(app)
    app.start()
    resumed = MockModel(script=[make_final("fine")])
    app2, _, _, _ = make_app(tmp_path, Lines("and now?", "/exit"), resumed)
    assert app2.start("latest") == 0
    assert "fine" in "".join(str(m.get("content")) for m in app2.session.history)
    assert SUMMARY_TAG in json.dumps(app2.session.history)


# -- folder settings -----------------------------------------------------------------------------------------

def test_a_newly_trusted_folder_gets_its_own_settings_file(tmp_path):
    model = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/exit"), model, newly_trusted=True)
    assert app.start() == 0
    path = cwd / ".agentx" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"model": app.model_label} and app.project_settings_created == path
    assert "Created" in out.getvalue() and "--folder" in out.getvalue()


def test_an_already_trusted_folder_is_left_alone(tmp_path):
    model = MockModel(script=[])
    app, _, cwd, _ = make_app(tmp_path, Lines("/exit"), model)
    assert app.start() == 0
    assert not (cwd / ".agentx" / "settings.json").exists()


def test_an_existing_folder_settings_file_is_never_overwritten_on_trust(tmp_path):
    model = MockModel(script=[])
    app, _, cwd, _ = make_app(tmp_path, Lines("/exit"), model, newly_trusted=True)
    (cwd / ".agentx").mkdir()
    (cwd / ".agentx" / "settings.json").write_text('{"model": "mine"}', encoding="utf-8")
    assert app.start() == 0
    assert json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8")) == {"model": "mine"}


def test_model_folder_saves_the_choice_for_this_folder(tmp_path):
    model = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/model gpt-4o-mini --folder", "/exit"), model)
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data == {"model": "gpt-4o-mini"} and "Created" in out.getvalue()


def test_model_folder_leaves_a_damaged_settings_file_alone(tmp_path):
    model = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/model gpt-4o-mini --folder", "/exit"), model)
    (cwd / ".agentx").mkdir()
    (cwd / ".agentx" / "settings.json").write_text("{not json", encoding="utf-8")
    assert app.start() == 0
    assert (cwd / ".agentx" / "settings.json").read_text(encoding="utf-8") == "{not json"
    assert "not valid JSON" in out.getvalue()


def test_the_log_title_for_init_and_team_commands_is_the_command(tmp_path):
    model = MockModel(script=[make_final("ok")])
    app, _, cwd, _ = make_app(tmp_path, Lines("/team survey the project", "/exit"), model, mode="auto")
    app.start()
    assert " - /team survey the project" in log_text(cwd)


def test_main_tells_the_app_whether_the_folder_was_trusted_before_this_run(tmp_path, monkeypatch):
    from agentx_dev.cli.trust import remember_trust

    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    seen = []

    class FakeApp:
        def __init__(self, **kw):
            seen.append(kw.get("newly_trusted"))

        def start(self, resume=None):
            return 0

    class FakePrompt:
        def __init__(self, **kw):
            self.read = self.read_short = lambda prompt="": ""
            self.select = lambda *args, **kwargs: None

    monkeypatch.setattr(app_module, "is_terminal", lambda: True)
    monkeypatch.setattr(app_module, "ChatApp", FakeApp)
    monkeypatch.setattr(app_module, "PromptInput", FakePrompt)
    monkeypatch.setattr(app_module, "setup_logging", lambda h: None)
    # The user answers yes: the folder is on file once ensure_trusted returns.
    monkeypatch.setattr(app_module, "ensure_trusted", lambda cwd, home, *a, **k: remember_trust(cwd, home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    assert app_module.main([]) == 0 and app_module.main([]) == 0
    assert seen == [True, False]
