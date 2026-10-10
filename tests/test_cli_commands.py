"""Slash commands, against a fake context."""

import pytest

from agentx_dev.cli.approval import Gate
from agentx_dev.cli.commands import command_names, parse_slash, run_command
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
    assert command_names() == ["/help", "/clear", "/model", "/mode", "/resume", "/sessions", "/init", "/exit"]


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


@pytest.mark.parametrize("word", ["bypass", "bypassPermissions", "yolo", "auto", "plan now", "default;plan", ""])
def test_mode_cannot_reach_anything_but_the_known_modes(word):
    ctx = Ctx()
    run_command(ctx, f"/mode {word}")
    assert ctx.gate.mode in ("default",)  # unchanged ("" only shows the mode)


def test_a_slash_alone_is_an_unknown_command_not_a_crash():
    ctx = Ctx()
    assert run_command(ctx, "/") is True
    assert ctx.errors and "Unknown command" in ctx.errors[0]


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
    run_command(ctx, "/\x1b[31m[red]boom" + "x" * 500)
    text = ctx.errors[0]
    assert "\x1b" not in text and len(text) < 300


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
