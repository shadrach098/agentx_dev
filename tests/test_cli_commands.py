"""Slash commands, against a fake context."""

import pytest

from agentx_dev.cli.approval import Gate
from agentx_dev.cli.commands import shown, command_names, parse_slash, run_command
from agentx_dev.cli.models import ModelError


class Ctx:
    def __init__(self):
        self.gate = Gate()
        self.model_label = "gpt-4o"
        self.said, self.errors, self.calls = [], [], []

    def notice(self, text): self.said.append(text)
    def error(self, text): self.errors.append(text)
    def new_conversation(self): self.calls.append(("clear",))
    def show_sessions(self): self.calls.append(("sessions",))
    def resume(self, ident): self.calls.append(("resume", ident))
    def run_init(self): self.calls.append(("init",))
    def run_team_command(self, task): self.calls.append(("team", task))
    def request_exit(self): self.calls.append(("exit",))

    def switch_model(self, name):
        if name == "bad":
            raise ModelError("no such model")
        self.calls.append(("model", name))
        self.model_label = name


def test_parse_slash():
    assert parse_slash("/mode plan") == ("mode", "plan")
    assert parse_slash("  /Help  ") == ("help", "")
    assert parse_slash("/resume   abc123 ") == ("resume", "abc123")
    assert parse_slash("fix the bug") is None
    assert parse_slash("") is None


def test_a_normal_message_is_not_a_command():
    assert run_command(Ctx(), "fix the bug") is False


def test_help_lists_every_command():
    ctx = Ctx()
    assert run_command(ctx, "/help") is True
    text = "\n".join(ctx.said)
    assert all(name in text for name in command_names())
    assert command_names() == ["/help", "/clear", "/model", "/mode", "/settings", "/resume", "/sessions", "/init", "/team", "/team-models", "/skills", "/exit"]


def test_clear_exit_init_sessions():
    ctx = Ctx()
    for line in ("/clear", "/sessions", "/init", "/exit"):
        run_command(ctx, line)
    assert ctx.calls == [("clear",), ("sessions",), ("init",), ("exit",)]


def test_model_shows_or_switches():
    ctx = Ctx()
    run_command(ctx, "/model")
    assert "gpt-4o" in ctx.said[-1]
    run_command(ctx, "/model claude-x")
    assert ("model", "claude-x") in ctx.calls and "claude-x" in ctx.said[-1]
    run_command(ctx, "/model bad")
    assert ctx.errors == ["no such model"]


def test_mode_shows_sets_and_rejects():
    ctx = Ctx()
    run_command(ctx, "/mode")
    assert "default" in ctx.said[-1] and "plan" in ctx.said[-1]
    run_command(ctx, "/mode plan")
    assert ctx.gate.mode == "plan"
    run_command(ctx, "/mode yolo")
    assert ctx.gate.mode == "plan" and "mode must be one of" in ctx.errors[-1]


def test_resume_passes_the_id_or_none():
    ctx = Ctx()
    run_command(ctx, "/resume")
    run_command(ctx, "/resume abc12345")
    assert ctx.calls == [("resume", None), ("resume", "abc12345")]


def test_an_unknown_command_lists_the_known_ones():
    ctx = Ctx()
    assert run_command(ctx, "/frobnicate now") is True
    assert "Unknown command /frobnicate" in ctx.errors[0] and "/help" in ctx.errors[0]


# ---- hardening ----------------------------------------------------------------------------------------

def test_mode_is_case_insensitive_and_shows_the_result():
    ctx = Ctx()
    run_command(ctx, "/mode ACCEPT-EDITS")
    assert ctx.gate.mode == "accept-edits" and "accept-edits" in ctx.said[-1]


@pytest.mark.parametrize("word", ["bypass", "bypassPermissions", "yolo", "plan now", "default;plan", ""])
def test_mode_cannot_reach_anything_but_the_known_modes(word):
    ctx = Ctx()
    run_command(ctx, f"/mode {word}")
    assert ctx.gate.mode in ("default",)  # unchanged ("" only shows the mode)


def test_a_slash_alone_or_followed_by_a_space_is_a_message():
    ctx = Ctx()
    for line in ("/", "/ is the root dir, why?", "/ mode plan"):
        assert parse_slash(line) is None
        assert run_command(ctx, line) is False
    assert not ctx.errors and ctx.gate.mode == "default"


@pytest.mark.parametrize("line", ["/README.md please summarise", "/a.b", "/[31m[red]boom", "/mode! plan", "/café"])
def test_a_first_word_with_odd_characters_is_a_message(line):
    assert parse_slash(line) is None


def test_names_stay_case_insensitive_and_allow_dash_and_underscore():
    assert parse_slash("/MODE plan") == ("mode", "plan")
    assert parse_slash("/accept-edits_1 x") == ("accept-edits_1", "x")


def test_whitespace_other_than_a_space_separates_the_argument():
    ctx = Ctx()
    run_command(ctx, "/mode\tplan")
    assert ctx.gate.mode == "plan"
    assert parse_slash("/resume\nabc") == ("resume", "abc")


def test_a_path_is_a_message_not_a_command():
    ctx = Ctx()
    assert run_command(ctx, "/tmp/build.log fails, why?") is False
    assert run_command(ctx, "/home/me/app.py") is False
    assert not ctx.errors


def test_unknown_command_text_is_neutralised_and_short():
    ctx = Ctx()
    assert run_command(ctx, "/" + "x" * 500 + " now") is True
    text = ctx.errors[0]
    assert "Unknown command" in text and len(text) < 300


def test_a_failing_handler_is_reported_on_one_line_and_never_raises():
    class Broken(Ctx):
        def new_conversation(self): raise RuntimeError("disk\nfull sk-abcdefghijklmnop1234")

        def show_sessions(self): raise OSError("unreadable settings")

    ctx = Broken()
    assert run_command(ctx, "/clear") is True
    assert run_command(ctx, "/sessions") is True
    assert len(ctx.errors) == 2
    assert all("\n" not in e for e in ctx.errors)
    assert "sk-abcdefghijklmnop1234" not in ctx.errors[0]
    assert "/clear" in ctx.errors[0] and "RuntimeError" in ctx.errors[0]


def test_a_non_model_error_from_switch_model_is_contained():
    class Ctx2(Ctx):
        def switch_model(self, name): raise KeyError("boom")

    ctx = Ctx2()
    assert run_command(ctx, "/model whatever") is True
    assert ctx.errors and "/model" in ctx.errors[0]


def test_model_error_text_is_neutralised():
    class Ctx2(Ctx):
        def switch_model(self, name): raise ModelError("bad\x1b[2J\nname")

    ctx = Ctx2()
    run_command(ctx, "/model x")
    assert "\x1b" not in ctx.errors[0] and "\n" not in ctx.errors[0]


def test_model_label_is_neutralised_when_shown():
    ctx = Ctx()
    ctx.model_label = "gpt\x1b[2J-4o"
    run_command(ctx, "/model")
    assert "\x1b" not in ctx.said[-1]


def test_a_failing_error_channel_does_not_escape():
    class Mute(Ctx):
        def error(self, text): raise RuntimeError("console gone")

        def new_conversation(self): raise RuntimeError("x")

    assert run_command(Mute(), "/clear") is True
    assert run_command(Mute(), "/nope") is True


def test_interruption_is_not_swallowed():
    class Interrupted(Ctx):
        def resume(self, ident): raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_command(Interrupted(), "/resume")


def test_extra_text_after_argument_free_commands_is_ignored():
    ctx = Ctx()
    run_command(ctx, "/clear everything please")
    assert ctx.calls == [("clear",)]


# ---- redaction ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "sk-abcdefghijkl", "sk-ant-api03-abcdefghijkl", "sk_live_abcdefghijkl", "pk_test_abcdefghijkl",
    "ghp_abcdefghijklmnop1234", "gho_abcdefghijklmnop1234",
    "xoxb-123456789-abcdefghij", "xoxa-123456789-abcdefghij", "xoxp-123456789-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE", "AIzaSyA-abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0",
    "a" * 40, "0123456789abcdef" * 3,
    "key-abcdefghij1", "tok-abcdefghijklmnopqrst",
    "Bearer abcdefghijklmnop1234", "Bearer abc.def-ghi_jkl~mno+pqr/stu=",
])
def test_secret_shaped_text_is_hidden(secret):
    out = shown(f"failed with {secret} at start")
    assert secret not in out and "[hidden]" in out and "failed with" in out


@pytest.mark.parametrize("line", [
    "api_key=abc123", "apikey=abc123", "API_KEY = abc123", "password=hunter2", "secret=shh", "token=abc",
    "OPENAI_API_KEY=whatever", "access_token=xyz",
])
def test_name_value_secrets_are_hidden(line):
    out = shown(f"bad {line} here")
    assert "[hidden]" in out and line.split("=")[-1].strip() not in out.replace("[hidden]", "")


@pytest.mark.parametrize("text", [
    "Bearer authentication failed", "api-key-configuration", "key-value-store", "tok-bucket",
    "the token is expired", "password policy", "a" * 39, "disk-quota-exceeded",
])
def test_ordinary_text_is_left_alone(text):
    assert shown(text) == text


def test_clear_help_line_is_neutral():
    ctx = Ctx()
    run_command(ctx, "/help")
    assert "/clear     Start a new conversation." in ctx.said[0]


def test_settings_asks_the_app_to_show_what_is_in_force():
    ctx = Ctx()
    ctx.show_settings = lambda: ctx.calls.append(("settings",))
    assert run_command(ctx, "/settings") is True
    assert ctx.calls == [("settings",)]
    assert "/settings" in command_names()


def test_model_tells_where_to_change_the_default():
    ctx = Ctx()
    ctx.user_settings_file = "C:/Users/me/.agentx/settings.json"
    run_command(ctx, "/model")
    assert "settings.json" in ctx.said[-1] and "gpt-4o-mini" in ctx.said[-1]
    run_command(ctx, "/model gpt-4o-mini")
    assert "this session only" in ctx.said[-1] and "settings.json" in ctx.said[-1]


def test_mode_auto_can_be_set_and_explains_itself():
    ctx = Ctx()
    run_command(ctx, "/mode auto")
    assert ctx.gate.mode == "auto"
    assert ctx.said[0] == "Mode: auto" and "Delete and move still ask" in ctx.said[1]


def test_team_sends_the_task_to_the_app():
    ctx = Ctx()
    assert run_command(ctx, "/team survey the project and fix the tests") is True
    assert ctx.calls == [("team", "survey the project and fix the tests")]


def test_team_without_a_task_explains_how_to_use_it():
    ctx = Ctx()
    assert run_command(ctx, "/team") is True
    assert ctx.calls == [] and "/team <task>" in ctx.said[-1]


def test_model_folder_switches_and_saves_for_the_folder():
    ctx = Ctx()
    ctx.save_folder_model = lambda name: ctx.calls.append(("folder", name))
    run_command(ctx, "/model gpt-4o-mini --folder")
    assert ctx.calls == [("model", "gpt-4o-mini"), ("folder", "gpt-4o-mini")]


def test_model_folder_flag_can_come_first_and_needs_a_name():
    ctx = Ctx()
    ctx.save_folder_model = lambda name: ctx.calls.append(("folder", name))
    run_command(ctx, "/model --folder gpt-4o")
    assert ctx.calls == [("model", "gpt-4o"), ("folder", "gpt-4o")]
    ctx = Ctx()
    run_command(ctx, "/model --folder")
    assert ctx.calls == [] and "/model <name> --folder" in ctx.errors[-1]


def test_a_model_that_cannot_be_built_is_not_saved_for_the_folder():
    ctx = Ctx()
    ctx.save_folder_model = lambda name: ctx.calls.append(("folder", name))
    run_command(ctx, "/model bad --folder")
    assert ctx.calls == [] and ctx.errors


def test_plain_model_stays_session_only_and_mentions_the_folder_flag():
    ctx = Ctx()
    ctx.user_settings_file = "C:/Users/me/.agentx/settings.json"
    run_command(ctx, "/model gpt-4o-mini")
    assert "this session only" in ctx.said[-1] and "--folder" in ctx.said[-1]
    # The user file is only the default for folders with no file of their own: a folder's file wins.
    assert "for every folder" not in ctx.said[-1]
    assert "without their own .agentx/settings.json" in ctx.said[-1] and "wins" in ctx.said[-1]


def test_hide_secrets_hides_key_shaped_tokens_and_keeps_newlines():
    from agentx_dev.cli.commands import hide_secrets
    text = hide_secrets("key sk-abcdefghijklmnop1234\nsecond line")
    assert "sk-abcdefghijklmnop1234" not in text and "[hidden]" in text and "\nsecond line" in text


def test_team_models_without_arguments_shows_the_list():
    ctx = Ctx()
    ctx.show_team_models = lambda: ctx.calls.append(("show-team",))
    run_command(ctx, "/team-models")
    assert ctx.calls == [("show-team",)]


def test_team_models_with_names_sets_them_for_the_session_or_the_folder():
    ctx = Ctx()
    ctx.set_team_models = lambda names, folder=False: ctx.calls.append(("set-team", list(names), folder))
    run_command(ctx, "/team-models claude-opus-5-5 gpt-5.5")
    run_command(ctx, "/team-models gpt-5.5 --folder")
    run_command(ctx, "/team-models clear")
    assert ctx.calls == [("set-team", ["claude-opus-5-5", "gpt-5.5"], False), ("set-team", ["gpt-5.5"], True),
                         ("set-team", [], False)]


def test_team_models_folder_needs_names_or_clear():
    ctx = Ctx()
    run_command(ctx, "/team-models --folder")
    assert "Usage: /team-models" in ctx.errors[-1]


def test_team_models_is_listed_in_help():
    from agentx_dev.cli.commands import command_names
    assert "/team-models" in command_names()


def test_team_models_keeps_quoted_notes_together():
    ctx = Ctx()
    ctx.set_team_models = lambda names, folder=False: ctx.calls.append(("set-team", list(names), folder))
    run_command(ctx, '/team-models gpt-6.1-sol="hard coding and bugs" gpt-5.5 --folder')
    assert ctx.calls == [("set-team", ["gpt-6.1-sol=hard coding and bugs", "gpt-5.5"], True)]


def test_team_models_with_an_unclosed_quote_shows_the_usage():
    ctx = Ctx()
    run_command(ctx, '/team-models gpt-6.1-sol="hard coding')
    assert ctx.errors and "Usage: /team-models" in ctx.errors[-1]


def test_an_unclosed_quote_is_named_in_the_error():
    ctx = Ctx()
    run_command(ctx, "/team-models gpt-5.5=user's pick")
    assert "quote" in ctx.errors[-1].lower()
