"""A team job through the whole app: the main agent calls start_team_job, the team runs, the result is in the
conversation. Scripted models only."""

import json

from agentx_dev.cli import app as app_module
from agentx_dev.cli.sessions import list_sessions
from agentx_dev.cli.team import build_helpers
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import PLANNER_MARK, SYNTH_MARK, new_agent_step, plan_json, step
from tests.test_cli_app import Lines, make_app

NO_TEAM_SENTENCE = ("You cannot start a team job; ignore any instruction above to call start_team_job and do "
                    "your step yourself.")
CALL = {"task": "Survey the project and report", "why": "it spans many areas"}


def team_model(steps, synth="The survey is complete.", helper=None, main=("call", "final")):
    """One model that plays the main agent, the planner, the synthesizer and the helpers.

    ``main`` is what the main agent does on its successive model calls: "call" starts a team job, "final"
    answers. After the list is used up it answers."""
    state = {"main": 0, "helper_calls": 0}

    def script(messages):
        first = str(messages[0]["content"])
        if "A coding assistant can run helper steps on these models" in str(messages[-1]["content"]):
            return "{}"                                  # the question about helper models: no notes
        if PLANNER_MARK in first:
            return plan_json(*steps)
        if SYNTH_MARK in first:
            return synth
        if "Team role:" in first:
            state["helper_calls"] += 1
            return (helper or (lambda messages, n: make_final("helper report")))(messages, state["helper_calls"])
        state["main"] += 1
        action = main[state["main"] - 1] if state["main"] <= len(main) else "final"
        if action == "call":                    # a later batch names what remains, as a real agent would
            batch = state["main"]
            return make_react_response("start_team_job", CALL if batch == 1 else
                                       {**CALL, "task": f"{CALL['task']} (batch {batch})"})
        return make_final("The team finished; here is the summary.")

    model = MockModel(script=script)
    model.state = state
    return model


def history_text(app):
    return json.dumps(app.session.history, default=str)


def test_the_main_agent_hands_a_big_job_to_the_team_and_the_result_is_in_the_conversation(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, home = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=Lines("y"))
    assert app.start() == 0
    text = out.getvalue()
    assert "Team plan" in text and "-> explorer: look around" in text and "done (explorer)" in text
    assert "The team finished; here is the summary." in text
    assert "Team job finished (outcome: done)" in history_text(app)          # the tool result is in the chat
    saved = list_sessions(home, app.cwd)
    assert saved and "Team job finished" in saved[0].path.read_text(encoding="utf-8")


def test_the_user_can_decline_the_hand_off(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=Lines("n", ""))
    assert app.start() == 0
    assert "Team plan" not in out.getvalue()
    assert "declined" in history_text(app).lower()
    assert model.state["helper_calls"] == 0


def test_plan_mode_refuses_the_hand_off_without_asking(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="plan",
                              read_short=lambda prompt="": asked.append(prompt) or "y")
    assert app.start() == 0
    assert "Team plan" not in out.getvalue() and asked == []
    assert "plan mode" in history_text(app)


def test_a_deny_rule_turns_the_feature_off(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, deny=["start_team_job:*"],
                              read_short=Lines("y"))
    assert app.start() == 0
    assert "Team plan" not in out.getvalue() and "deny rule" in history_text(app)


def test_auto_mode_starts_the_team_without_asking(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              read_short=lambda prompt="": asked.append(prompt) or "n")
    assert app.start() == 0
    assert "Team plan" in out.getvalue() and asked == []


def test_a_message_may_run_up_to_five_team_jobs_then_the_next_is_refused(tmp_path):
    model = team_model([step("s1", "explorer", "look around")], main=("call",) * 6 + ("final",))
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == app_module.MAX_TEAM_JOBS_PER_MESSAGE == 5
    assert "5 team jobs already ran for this message" in history_text(app)


def test_the_next_message_may_start_another_job(tmp_path):
    model = team_model([step("s1", "explorer", "look around")], main=("call", "final", "call", "final"))
    app, out, _, _ = make_app(tmp_path, Lines("first big job", "second big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == 2
    assert "already ran for this message" not in history_text(app)


def test_a_helper_edit_still_asks_in_default_mode_and_the_prompt_names_the_helper(tmp_path):
    def coder(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": "team.txt", "content": "hi", "if_exists": "refuse"})
        return make_final("wrote team.txt")

    model = team_model([step("s1", "coder", "write team.txt")], helper=coder)
    app, out, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model,
                                read_short=Lines("y", "y"))                 # the hand-off, then the write
    assert app.start() == 0
    assert (cwd / "team.txt").read_text(encoding="utf-8") == "hi"
    text = out.getvalue()
    assert "Helper: coder" in text and "[coder] write_file" in text
    assert text.index("[coder] write_file") < text.index("Helper: coder")      # the call shows before its prompt


def test_a_helper_cannot_write_where_the_default_denies_even_in_auto_mode(tmp_path):
    told = []

    def coder(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": ".git/config", "content": "x",
                                                       "if_exists": "overwrite"})
        told.append(str(messages[-1]["content"]))               # what the helper was told about its call
        return make_final("tried")

    (tmp_path / "proj" / ".git").mkdir(parents=True)
    model = team_model([step("s1", "coder", "edit git config")], helper=coder)
    app, out, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert not (cwd / ".git" / "config").exists()
    assert told and "deny rule" in told[0]


def test_the_explorer_has_no_way_to_write(tmp_path):
    def explorer(messages, n):
        if n == 1:
            return make_react_response("write_file", {"path": "x.txt", "content": "hi", "if_exists": "refuse"})
        return make_final("could not write")

    model = team_model([step("s1", "explorer", "try to write")], helper=explorer)
    app, _, cwd, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert not (cwd / "x.txt").exists()
    assert model.state["helper_calls"] >= 1                     # the explorer did run and did try


def test_the_planner_can_ask_the_user_and_an_empty_answer_skips_it(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    original = model._script_fn

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first and not asked:
            asked.append(True)
            return json.dumps({"plan": [], "ask": [{"question": "Which folder?", "why": "to scope the survey"}]})
        return original(messages)

    model._script_fn = script
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              read_short=Lines(""))
    assert app.start() == 0
    assert "Which folder?" in out.getvalue()
    assert "Team plan" in out.getvalue()                        # the job went on after the skipped question
    assert "OPERATOR ANSWERS" not in " ".join(str(call) for call in model.calls)     # nothing was passed on


def test_a_typed_answer_reaches_the_planner(tmp_path):
    asked = []
    model = team_model([step("s1", "explorer", "look around")])
    original = model._script_fn

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first and not asked:
            asked.append(True)
            return json.dumps({"plan": [], "ask": [{"question": "Which folder?", "why": "to scope the survey"}]})
        return original(messages)

    model._script_fn = script
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              read_short=Lines("the docs folder"))
    assert app.start() == 0
    assert "A: the docs folder" in " ".join(str(call) for call in model.calls)


def test_ctrl_c_in_the_middle_of_a_job_aborts_the_turn_and_the_next_message_works(tmp_path):
    def helper(messages, n):
        raise KeyboardInterrupt

    model = team_model([step("s1", "explorer", "look around")], helper=helper)
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "next message", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert "Turn interrupted" in out.getvalue() and "here is the summary" in out.getvalue()
    assert app._team_running == []


def test_team_command_asks_the_main_agent_to_call_the_tool(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("/team survey the project", "/exit"), model, mode="auto")
    assert app.start() == 0
    sent = " ".join(str(message.get("content")) for call in model.calls for message in call
                    if message.get("role") == "user")
    assert "start_team_job" in sent and "survey the project" in sent
    assert "Team plan" in out.getvalue()


def test_the_team_tool_is_in_the_main_tool_list_but_not_in_any_helper(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), model)
    names = [tool.name for tool in app.tools]
    assert names.count("start_team_job") == 1 and "recall" in names and len(names) == len(app.base_tools) + 2
    assert "start_team_job" not in [tool.name for tool in app.base_tools]
    for name, (_, runner) in build_helpers(model, app.base_tools, app.addendum).items():
        assert "start_team_job" not in [tool.name for tool in runner.tools], name


def test_helpers_are_told_they_cannot_start_a_team_although_the_chat_prompt_says_to(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), model)
    assert "start_team_job" in app.addendum                     # the chat's own prompt tells the model to use it
    helpers = build_helpers(model, app.tools, app.addendum)     # even if handed the whole tool list
    assert set(helpers) == {"explorer", "coder", "runner"}
    for name, (_, runner) in helpers.items():
        assert NO_TEAM_SENTENCE in runner.system_addendum, name
        assert "start_team_job" not in [tool.name for tool in runner.tools], name


def test_a_job_past_the_limit_is_refused_before_the_user_is_asked_again(tmp_path):
    asked = []

    def read_short(prompt=""):
        asked.append(prompt)
        return "y"

    model = team_model([step("s1", "explorer", "look around")], main=("call",) * 6 + ("final",))
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=read_short)
    assert app.start() == 0
    assert len(asked) == 5                                      # each job asked once; the sixth never asked
    assert out.getvalue().count("Team plan:") == 5
    assert "already ran for this message" in history_text(app)


def test_a_declined_hand_off_may_be_asked_again_in_the_same_turn(tmp_path):
    answers = Lines("n", "", "y")
    model = team_model([step("s1", "explorer", "look around")], main=("call", "call", "final"))
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, read_short=answers)
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == 1 and "already ran for this message" not in history_text(app)


def test_an_error_text_with_the_api_key_never_reaches_the_result_string(tmp_path, monkeypatch):
    secret = "sk-test-0123456789abcdef"
    model = team_model([step("s1", "explorer", "look around")])

    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                              env={"OPENAI_API_KEY": secret})
    # a failure outside the helpers' own error handling: the synthesizer's model call raises
    original = model._script_fn

    def script(messages):
        if SYNTH_MARK in str(messages[0]["content"]):
            raise RuntimeError(f"401 for key {secret} \x1b[2J")
        return original(messages)

    model._script_fn = script
    assert app.start() == 0
    history = history_text(app)
    assert "Team job failed (RuntimeError)" in history
    assert secret not in history and secret not in out.getvalue()
    assert "[hidden]" in history


def test_a_helper_error_with_the_api_key_in_a_url_never_reaches_the_screen_the_chat_or_any_model(tmp_path):
    secret = "sk-test-0123456789abcdef"

    def explorer(messages, n):
        raise RuntimeError(f"400 for url https://x/?key={secret}")

    model = team_model([step("s1", "explorer", "look around")], helper=explorer)
    app, out, _, home = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto",
                                 env={"OPENAI_API_KEY": secret})
    assert app.start() == 0
    assert model.state["helper_calls"] >= 1
    everything = out.getvalue() + history_text(app) + " ".join(str(call) for call in model.calls)
    assert secret not in everything and "failed (explorer)" in out.getvalue()
    saved = list_sessions(home, app.cwd)
    assert saved and secret not in saved[0].path.read_text(encoding="utf-8")


def test_a_planned_new_agent_step_never_runs_a_helper(tmp_path):
    model = team_model([new_agent_step("s1", "fixer", "Fix things.", query="do it")])
    app, _, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert model.state["helper_calls"] == 0


def test_a_resumed_session_still_holds_the_job_result(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, _, _, home = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    ident = list_sessions(home, app.cwd)[0].session_id[:8]
    second = team_model([step("s1", "explorer", "look around")], main=("final",))
    app2, _, _, _ = make_app(tmp_path, Lines(f"/resume {ident}", "/exit"), second, mode="auto")
    assert app2.start() == 0
    assert "Team job finished (outcome: done)" in history_text(app2)


def test_the_tool_handles_a_missing_model_and_a_repeat_call_directly(tmp_path):
    model = team_model([step("s1", "explorer", "look around")])
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), model)
    assert "no model" in app._start_team_job("task", "why")
    app.model = model
    app._team_jobs = app_module.MAX_TEAM_JOBS_PER_MESSAGE
    assert app._start_team_job("task", "why") == app_module.TEAM_LIMIT_REACHED


from tests.conftest import MockModel as _Mock

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789", "OPENAI_API_KEY": "sk-test-0123456789abcdef"}


def extra(name, reply="opus report"):
    model = _Mock(script=lambda messages: make_final(reply))
    model.model_name = name
    return model


def factory_for(chat, **extras):
    def factory(name, env):
        return extras[name] if name in extras else chat
    return factory


def test_a_step_runs_on_the_model_the_planner_picked(tmp_path):
    chat = team_model([step("s1", "coder@claude-opus-5-5", "make the change")])
    chat.model_name = "claude-sonnet-5-5"
    opus = extra("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory_for(chat, **{"claude-opus-5-5": opus}),
                              team_models=["claude-opus-5-5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5" in text        # in the approval panel
    assert "-> coder@claude-opus-5-5: make the change" in text
    assert opus.calls and chat.state["helper_calls"] == 0
    assert "coder@claude-opus-5-5 - done" in history_text(app)


def test_a_model_without_a_key_is_not_used_and_the_job_still_runs(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"),
                              env={"ANTHROPIC_API_KEY": KEYS["ANTHROPIC_API_KEY"]},
                              team_models=["gpt-5.5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: gpt-5.5 (no OPENAI_API_KEY)" in text and "done (explorer)" in text


def test_a_model_that_cannot_be_built_shows_only_the_error_type(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-5-5"

    def factory(name, env):
        if name == "gpt-5.5":
            raise ValueError(f"bad {KEYS['OPENAI_API_KEY']}")
        return chat

    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory, team_models=["gpt-5.5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: gpt-5.5 (could not be set up: ValueError)" in text and KEYS["OPENAI_API_KEY"] not in text


def test_a_model_error_while_building_shows_its_message_and_no_key(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-4-5"

    def factory(name, env):
        if name == "claude-opus-5-5":
            raise app_module.ModelError("No module named 'anthropic' - install agentx-dev[anthropic]")
        return chat

    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory, team_models=["claude-opus-5-5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: claude-opus-5-5 (No module named 'anthropic' - install agentx-dev[anthropic])" in text
    assert "done (explorer)" in text


def test_a_model_error_that_carries_a_key_is_still_hidden_by_the_app(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-4-5"

    def factory(name, env):
        if name == "gpt-5.5":
            raise app_module.ModelError(f"rejected {KEYS['OPENAI_API_KEY']}")
        return chat

    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory, team_models=["gpt-5.5"])
    assert app.start() == 0
    text = out.getvalue()
    assert "Not used: gpt-5.5 (rejected" in text and KEYS["OPENAI_API_KEY"] not in text


def test_the_spinner_is_stopped_before_the_not_used_notes(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"),
                              env={"ANTHROPIC_API_KEY": KEYS["ANTHROPIC_API_KEY"]}, team_models=["gpt-5.5"])
    events = []
    start, stop, notice = app.progress.start, app.progress.stop, app.notice
    app.progress.start = lambda *a, **k: (events.append("start"), start(*a, **k))[1]
    app.progress.stop = lambda *a, **k: (events.append("stop"), stop(*a, **k))[1]
    app.notice = lambda text: (events.append("not used" if "Not used" in text else "notice"), notice(text))[1]
    assert app.start() == 0
    at = events.index("not used")
    assert [e for e in events[:at] if e in ("start", "stop")][-1] == "stop"


def test_without_a_list_the_team_job_is_unchanged(tmp_path):
    chat = team_model([step("s1", "explorer", "look around")])
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"))
    assert app.start() == 0
    assert "Helper models" not in out.getvalue() and "Not used" not in out.getvalue()


def test_team_models_command_sets_shows_and_clears_for_the_session(tmp_path):
    chat = MockModel(script=[])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 mistral-x gpt-5.5", "/team-models",
                                              "/team-models clear", "/exit"), chat,
                              env={"ANTHROPIC_API_KEY": KEYS["ANTHROPIC_API_KEY"]})
    assert app.start() == 0
    text = out.getvalue()
    assert "mistral-x" in text                                        # the bad name is reported
    assert "claude-opus-5-5" in text and "Not used now: gpt-5.5 (no OPENAI_API_KEY)" in text
    assert app.team_models == []


def test_team_models_folder_saves_them_in_the_folder_file(tmp_path):
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["teamModels"] == ["claude-opus-5-5"] and app.team_models == ["claude-opus-5-5"]


def test_team_models_folder_is_refused_in_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "is_project_folder", lambda cwd, home: False)
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert not (cwd / ".agentx" / "settings.json").exists() and "home folder" in out.getvalue()


def test_settings_shows_the_helper_models(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/settings", "/exit"), chat, env=KEYS,
                              team_models=["claude-opus-5-5"])
    assert app.start() == 0
    assert "Helper models: claude-opus-5-5" in out.getvalue()


def test_a_hostile_model_name_is_printed_inert(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/team-models claude-[red]x[/red]\x1b[2J", "/team-models", "/exit"),
                              chat, env=KEYS)
    assert app.start() == 0
    assert "\x1b" not in out.getvalue()


# -- a repository's own helper models are announced; an empty list says it turned them off ---------------

def project_file(tmp_path):
    return tmp_path / "proj" / ".agentx" / "settings.json"


def test_a_list_from_the_project_settings_is_announced_at_startup(tmp_path):
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/exit"), chat, env=KEYS, team_models=["claude-opus-5-5", "gpt-5.5"],
                                team_models_source=project_file(tmp_path))
    assert app.start() == 0
    text = squash_ws(out.getvalue())
    assert "This project's settings let team helpers also use: claude-opus-5-5, gpt-5.5" in text
    assert "settings.json" in text and "/team-models clear turns that off for this session." in text


def test_the_announcement_hides_control_characters_and_keys(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), chat, env=KEYS,
                              team_models=["claude-[2J" + KEYS["OPENAI_API_KEY"]],
                              team_models_source=project_file(tmp_path))
    assert app.start() == 0
    assert "" not in out.getvalue() and KEYS["OPENAI_API_KEY"] not in out.getvalue()
    assert "This project's settings let team helpers also use" in out.getvalue()


def test_a_list_from_the_users_own_settings_is_not_announced(tmp_path):
    chat = MockModel(script=[])
    app, out, _, home = make_app(tmp_path, Lines("/exit"), chat, env=KEYS, team_models=["claude-opus-5-5"],
                                 team_models_source=tmp_path / "home" / ".agentx" / "settings.json")
    assert app.start() == 0
    assert "This project's settings" not in out.getvalue()


def test_no_list_means_no_announcement(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert "team helpers also use" not in out.getvalue()
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), chat, env=KEYS, team_models=[],
                              team_models_source=project_file(tmp_path))     # a project that turns them off
    assert app.start() == 0
    assert "team helpers also use" not in out.getvalue()


def test_a_folder_file_with_an_empty_list_says_it_turned_the_extra_models_off(tmp_path):
    chat = MockModel(script=[])
    chat.model_name = "claude-sonnet-5-5"
    app, out, _, _ = make_app(tmp_path, Lines("/team-models", "/exit"), chat, env=KEYS, team_models=[],
                              team_models_source=project_file(tmp_path))
    assert app.start() == 0
    text = squash_ws(out.getvalue())
    assert "Helper models: none - turned off by" in text and "settings.json" in text
    assert "none set" not in text


def test_without_any_file_the_list_is_none_set(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/team-models", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert "Helper models: none set" in squash_ws(out.getvalue()) and "turned off" not in out.getvalue()


def test_clearing_in_the_session_forgets_the_file(tmp_path):
    chat = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/team-models clear", "/exit"), chat, env=KEYS,
                              team_models=["claude-opus-5-5"], team_models_source=project_file(tmp_path))
    assert app.start() == 0
    assert app.team_models_source is None and "none set" in squash_ws(out.getvalue())


def test_team_models_folder_reports_the_file_as_the_source(tmp_path):
    chat = MockModel(script=[])
    app, out, cwd, _ = make_app(tmp_path, Lines("/team-models claude-opus-5-5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    text = squash_ws(out.getvalue())
    assert "Helper models (from " in text and "settings.json): claude-opus-5-5" in text
    assert "(this session)" not in text
    assert app.team_models_source == cwd.resolve() / ".agentx" / "settings.json"


def squash_ws(text):
    return " ".join(text.split())


def test_with_a_list_even_the_chat_model_helpers_name_their_model(tmp_path):
    chat = team_model([step("s1", "runner@claude-sonnet-5-5", "check it")])
    chat.model_name = "claude-sonnet-5-5"
    opus = extra("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory_for(chat, **{"claude-opus-5-5": opus}),
                              team_models=["claude-opus-5-5"])
    assert app.start() == 0
    assert "-> runner@claude-sonnet-5-5: check it" in out.getvalue() and not opus.calls


def test_notes_reach_the_planner_and_show_in_team_models(tmp_path):
    seen = []
    chat = team_model([step("s1", "coder@claude-opus-5-5", "make it")])
    chat.model_name = "claude-sonnet-5-5"
    original = chat._script_fn

    def spy(messages):
        seen.append(str(messages[0]["content"]))
        return original(messages)

    chat._script_fn = spy
    opus = extra("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines('/team-models claude-opus-5-5="hard coding"', "/team-models",
                                              "do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                              model_factory=factory_for(chat, **{"claude-opus-5-5": opus}))
    assert app.start() == 0
    assert "claude-opus-5-5 (hard coding)" in out.getvalue()
    assert any("Good for: hard coding." in text for text in seen)         # in the planner's helper catalog
    assert app.team_model_uses == {"claude-opus-5-5": "hard coding"}


def test_team_models_folder_saves_the_notes(tmp_path):
    chat = MockModel(script=[])
    app, _, cwd, _ = make_app(tmp_path, Lines('/team-models gpt-5.5="fast reading" --folder', "/exit"), chat,
                              env=KEYS)
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["teamModels"] == [{"model": "gpt-5.5", "use": "fast reading"}]


def test_a_note_on_the_chat_model_reaches_its_helpers(tmp_path):
    seen = []
    chat = team_model([step("s1", "runner@claude-sonnet-5-5", "check")])
    chat.model_name = "claude-sonnet-5-5"
    original = chat._script_fn
    chat._script_fn = lambda messages: seen.append(str(messages[0]["content"])) or original(messages)
    app, _, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), chat, read_short=Lines("y"), env=KEYS,
                            model_factory=factory_for(chat, **{"claude-opus-5-5": extra("claude-opus-5-5")}),
                            team_models=["claude-opus-5-5"], team_model_uses={"Claude-Sonnet-5-5": "everyday work"})
    assert app.start() == 0
    assert any("Runs on claude-sonnet-5-5. Good for: everyday work." in text for text in seen)


def test_project_notes_are_shown_at_startup_and_in_settings(tmp_path):
    from agentx_dev.cli.settings import project_settings_path
    chat = MockModel(script=[])
    cwd = tmp_path / "proj"
    app, out, cwd, _ = make_app(tmp_path, Lines("/settings", "/exit"), chat, env=KEYS,
                                team_models=["gpt-5.5"], team_model_uses={"gpt-5.5": "use for every step"},
                                team_models_source=project_settings_path(tmp_path / "proj"))
    assert app.start() == 0
    text = out.getvalue()
    assert "also use: gpt-5.5 (use for every step)" in text
    assert "Helper models: gpt-5.5 (use for every step)" in text


# -- notes written by the chat model ---------------------------------------------------------------------------

DESCRIBE_MARK = "A coding assistant can run helper steps on these models"


def describing_model(reply='{"gpt-5.5": "fast, cheaper coding", "gpt-6-luna": "unknown", '
                           '"claude-sonnet-5-5": "careful everyday coding"}', label="claude-sonnet-5-5"):
    asked = []

    def script(messages):
        text = str(messages[-1]["content"])
        if DESCRIBE_MARK in text:
            asked.append(text)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return make_final("ok")

    model = MockModel(script=script)
    model.model_name = label
    model.asked = asked
    return model


def test_setting_a_list_asks_the_chat_model_once_for_notes(tmp_path):
    chat = describing_model()
    app, out, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5 gpt-6-luna", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert len(chat.asked) == 1 and "gpt-5.5" in chat.asked[0] and "claude-sonnet-5-5" in chat.asked[0]
    assert app.team_model_uses["gpt-5.5"] == "auto: fast, cheaper coding"
    assert app._use_of("claude-sonnet-5-5") == "auto: careful everyday coding"
    assert "gpt-6-luna" not in app.team_model_uses                       # "unknown" gives no note
    assert "gpt-5.5 (auto: fast, cheaper coding)" in out.getvalue()


def test_a_note_you_wrote_is_never_asked_about_or_replaced_and_describe_refreshes_auto_notes(tmp_path):
    replies = ['{"gpt-5.5": "something else", "gpt-6-luna": "fast reading", "claude-sonnet-5-5": "careful"}',
               '{"gpt-5.5": "something else", "gpt-6-luna": "quick search", "claude-sonnet-5-5": "steady"}']
    chat = describing_model(reply=None)
    chat._script_fn = (lambda original: lambda messages: (chat.asked.append(str(messages[-1]["content"]))
                                                          or replies.pop(0))
                       if DESCRIBE_MARK in str(messages[-1]["content"]) else original(messages))(chat._script_fn)
    app, _, _, _ = make_app(tmp_path, Lines('/team-models gpt-5.5="mine" gpt-6-luna', "/team-models describe",
                                            "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert all("- gpt-5.5" not in text for text in chat.asked) and len(chat.asked) == 2
    assert app.team_model_uses["gpt-5.5"] == "mine" and app.team_model_uses["gpt-6-luna"] == "auto: quick search"
    assert app._use_of("claude-sonnet-5-5") == "auto: steady"           # the chat's own note survives describe


def test_a_failed_description_leaves_the_list_set_and_says_so(tmp_path):
    chat = describing_model(reply=RuntimeError("down"))
    app, out, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert app.team_models == ["gpt-5.5"] and "Could not describe the models" in out.getvalue()
    assert "down" not in out.getvalue()


def test_folder_saves_the_auto_notes(tmp_path):
    chat = describing_model()
    app, _, cwd, _ = make_app(tmp_path, Lines("/team-models gpt-5.5 --folder", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["teamModels"] == [{"model": "gpt-5.5", "use": "auto: fast, cheaper coding"}]


def test_clear_asks_nothing(tmp_path):
    chat = describing_model()
    app, _, _, _ = make_app(tmp_path, Lines("/team-models clear", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert chat.asked == []


def test_a_list_from_settings_is_described_at_the_first_job_only(tmp_path):
    described = []
    chat = team_model([step("s1", "explorer@claude-sonnet-5-5", "look")], main=("call", "final", "call", "final"))
    chat.model_name = "claude-sonnet-5-5"
    original = chat._script_fn

    def script(messages):
        if DESCRIBE_MARK in str(messages[-1]["content"]):
            described.append(1)
            return '{"claude-opus-5-5": "hard coding"}'
        return original(messages)

    chat._script_fn = script
    opus = extra("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "and another", "/exit"), chat,
                              read_short=Lines("y", "y"), env=KEYS, team_models=["claude-opus-5-5"],
                              model_factory=factory_for(chat, **{"claude-opus-5-5": opus}))
    assert app.start() == 0
    assert described == [1] and app.team_model_uses.get("claude-opus-5-5") == "auto: hard coding"


def test_setting_the_list_again_keeps_the_notes_already_written(tmp_path):
    chat = describing_model()
    app, _, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/team-models gpt-5.5 gpt-6-luna", "/exit"),
                            chat, env=KEYS)
    assert app.start() == 0
    assert app.team_model_uses["gpt-5.5"] == "auto: fast, cheaper coding"
    assert app._use_of("claude-sonnet-5-5") == "auto: careful everyday coding"
    assert len(chat.asked) == 2 and "- gpt-5.5" not in chat.asked[1]      # only the new model is asked about


def test_describe_keeps_a_file_list_marked_as_from_the_file(tmp_path):
    from agentx_dev.cli.settings import project_settings_path
    source = project_settings_path(tmp_path / "proj")
    chat = describing_model()
    app, out, _, _ = make_app(tmp_path, Lines("/team-models describe", "/exit"), chat, env=KEYS,
                              team_models=["gpt-5.5"], team_models_source=source)
    assert app.start() == 0
    assert app.team_models_source == source and "(from " in out.getvalue().split("/team-models describe")[-1]


def test_describe_folder_saves_the_notes(tmp_path):
    chat = describing_model()
    app, _, cwd, _ = make_app(tmp_path, Lines("/team-models describe --folder", "/exit"), chat, env=KEYS,
                              team_models=["gpt-5.5"])
    assert app.start() == 0
    data = json.loads((cwd / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["teamModels"] == [{"model": "gpt-5.5", "use": "auto: fast, cheaper coding"}]


def test_ctrl_c_skips_describing_and_an_unreadable_reply_says_so(tmp_path):
    chat = describing_model(reply=KeyboardInterrupt())
    app, out, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/exit"), chat, env=KEYS)
    assert app.start() == 0 and app.team_models == ["gpt-5.5"] and "Skipped describing" in out.getvalue()
    chat = describing_model(reply="I cannot answer that.")
    app, out, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/exit"), chat, env=KEYS)
    assert app.start() == 0 and "could not be read" in out.getvalue()


def test_team_models_shows_the_chat_models_note(tmp_path):
    chat = describing_model()
    app, out, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/exit"), chat, env=KEYS)
    assert app.start() == 0
    assert "claude-sonnet-5-5 (chat; auto: careful everyday coding)" in out.getvalue()


def test_describe_drops_an_auto_note_the_model_no_longer_stands_by(tmp_path):
    replies = ['{"gpt-5.5": "fast"}', '{"gpt-5.5": "unknown"}']
    chat = describing_model()
    original = chat._script_fn
    chat._script_fn = lambda messages: (replies.pop(0) if DESCRIBE_MARK in str(messages[-1]["content"])
                                        else original(messages))
    app, _, _, _ = make_app(tmp_path, Lines("/team-models gpt-5.5", "/team-models describe", "/exit"), chat,
                            env=KEYS)
    assert app.start() == 0
    assert "gpt-5.5" not in app.team_model_uses


def test_a_helper_that_runs_out_of_steps_hands_over_partial_work_and_the_next_step_runs(tmp_path, monkeypatch):
    from agentx_dev.cli import team as team_module
    monkeypatch.setattr(team_module, "HELPER_STEPS", 4)
    loops, seen_by_runner = [], []

    def helper(messages, n):                     # the coder never finishes; the runner does
        if "coder helper" in str(messages[0]["content"]):
            loops.append(n)
            return make_react_response("list_directory", {"path": f"dir{n}"})   # varied: not a stuck loop
        seen_by_runner.append(str(messages[-1]["content"]) + " ".join(str(m["content"]) for m in messages))
        return make_final("checked the coder's partial work")

    model = team_model([step("c1", "coder", "change it"), step("r1", "runner", "check it", ["c1"])], helper=helper)
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    text = out.getvalue()
    assert "skipped" not in text and "done (runner)" in text
    assert "coder - partial (ran out of steps)" in history_text(app)
    assert "partial (coder): (This helper ran out of steps" in text                # the live line says partial
    assert any("ran out of steps" in seen for seen in seen_by_runner)              # the runner is told


def test_a_job_where_no_step_finished_stops_the_next_one(tmp_path):
    def helper(messages, n):                   # every helper gets stuck repeating one call
        return make_react_response("list_directory", {"path": "."})

    model = team_model([step("s1", "explorer", "look")], helper=helper, main=("call", "call", "final"))
    app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert out.getvalue().count("Team plan:") == 1
    assert "no step of the last team job finished" in history_text(app)


def test_each_result_says_which_job_of_the_message_it_was(tmp_path):
    model = team_model([step("s1", "explorer", "look around")], main=("call", "call", "final"))
    app, _, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto")
    assert app.start() == 0
    assert "team job 1 of 5 for this message" in history_text(app)
    assert "team job 2 of 5 for this message" in history_text(app)
