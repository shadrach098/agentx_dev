"""The token limit of one message: past it, agentx asks before it goes on, and a no ends the turn like Ctrl+C.
Also /limit, the "tokenLimit" setting, and cached input in /usage."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from agentx_dev import AgentRunner, AgentType
from agentx_dev.ChatModel import _openai_cached_tokens
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.app import TokenLimitReached
from agentx_dev.cli.commands import command_names, parse_token_count, run_command
from agentx_dev.cli.settings import DEFAULT_TOKEN_LIMIT, load_settings
from agentx_dev.cli.team import _HelperRunner
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, make_app


def looking():
    return make_react_response("list_directory", {"path": "."})


def run(tmp_path, *lines, limit, script):
    model = MockModel(script=script)                    # every reply records 15 tokens (10 in, 5 out)
    answers = Lines(*lines, "/exit")
    app, out, cwd, home = make_app(tmp_path, answers, model, width=300, token_limit=limit)
    assert app.start() == 0
    return app, out, model, answers


class TestTheLimit:
    def test_past_the_limit_it_asks_and_a_no_wraps_up_with_what_was_found(self, tmp_path):
        from agentx_dev.cli.app import WRAP_UP
        app, out, model, answers = run(tmp_path, "look around", "n", limit=20,
                                       script=[looking(), looking(), make_final("Here is what I found so far.")])
        text = out.getvalue()
        assert any("This message has used 30 tokens" in p and "No wraps up" in p for p in answers.prompts)
        assert "Wrapping up" in text and "Here is what I found so far." in text
        assert WRAP_UP.split(".")[0] in str(model.calls[-1])          # the refused tool told the model to stop
        assert app.gate.closed is None                                 # the next message starts open
        history = app.session.history if hasattr(app.session, "history") else None
        assert "Stopped:" not in text and (history is None or "look around" in str(history))   # the turn is kept

    def test_wrapping_up_that_runs_past_its_allowance_is_stopped(self, tmp_path, monkeypatch):
        from agentx_dev.cli import app as app_module
        monkeypatch.setattr(app_module, "WRAP_UP_TOKENS", 10)
        app, out, model, answers = run(tmp_path, "look around", "n", limit=20,
                                       script=[looking(), looking(), looking(), looking(), make_final("never")])
        text = out.getvalue()
        assert "Stopped: this message used 45 tokens" in text and "never" not in text
        assert len(model.calls) == 3 and app.gate.closed is None

    def test_a_yes_goes_on_for_another_limit(self, tmp_path):
        app, out, model, answers = run(tmp_path, "look around", "y", limit=20,
                                       script=[looking(), looking(), make_final("all done")])
        assert "all done" in out.getvalue()
        assert sum("Keep going" in p for p in answers.prompts) == 1     # 45 tokens is within 30 + 20

    def test_a_goes_on_and_does_not_ask_again_in_this_message(self, tmp_path):
        app, out, model, answers = run(tmp_path, "look around", "a", limit=20,
                                       script=[*(make_react_response("grep", {"pattern": f"p{n}"}) for n in range(4)),
                                               make_final("all done")])      # four different calls: no loop guard
        assert "all done" in out.getvalue() and "[y/a/N]" in answers.prompts[1]
        assert sum("Keep going" in p for p in answers.prompts) == 1       # 75 tokens, asked once, at 30

    def test_an_unclear_answer_is_asked_once_more(self, tmp_path):
        app, out, model, answers = run(tmp_path, "look around", "maybe", "n", limit=20,
                                       script=[looking(), looking(), make_final("wrapped")])
        assert "Please answer y, a or n." in out.getvalue() and "wrapped" in out.getvalue()

    def test_no_limit_never_asks(self, tmp_path):
        app, out, model, answers = run(tmp_path, "look around", limit=0,
                                       script=[looking(), looking(), make_final("all done")])
        assert "all done" in out.getvalue() and not any("Keep going" in p for p in answers.prompts)

    def test_each_message_starts_again_from_zero(self, tmp_path):
        app, out, model, answers = run(tmp_path, "one", "two", limit=20,
                                       script=[make_final("first"), make_final("second")])
        text = out.getvalue()
        assert "first" in text and "second" in text and not any("Keep going" in p for p in answers.prompts)

    def test_outside_a_turn_the_hook_does_nothing(self, tmp_path):
        app, out, model, answers = run(tmp_path, limit=1, script=[])
        model._record_usage_counts(input_tokens=500, output_tokens=5)      # calls the hook; must not raise
        app._check_token_limit(model)

    def test_the_default_is_on(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        assert app.token_limit == DEFAULT_TOKEN_LIMIT == 200_000


class TestTheStopGetsThrough:
    """The stop is a KeyboardInterrupt, so tool dispatch and team helpers let it through like Ctrl+C."""

    def test_it_is_a_keyboard_interrupt(self):
        assert issubclass(TokenLimitReached, KeyboardInterrupt)

    def test_tool_dispatch_does_not_turn_it_into_a_tool_error(self):
        def boom(path: str) -> str:
            raise TokenLimitReached(100, 50)
        class Args(BaseModel):
            path: str

        tool = StructuredTool(func=boom, args_schema=Args, name="boom", description="d")
        runner = AgentRunner(model=MockModel(script=[make_react_response("boom", {"path": "x"}),
                                                     make_final("never")]),
                             agent=AgentType.ReAct, tools=[tool], verbose=False)
        with pytest.raises(TokenLimitReached):
            runner.Initialize("go")

    def test_a_team_helper_lets_it_through(self):
        model = MockModel(script=[make_final("x")])
        model.on_usage = lambda m: (_ for _ in ()).throw(TokenLimitReached(10, 5))
        helper = _HelperRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                               describe_error=lambda e: "hidden")
        with pytest.raises(TokenLimitReached):
            helper.Initialize("go")


class TestLimitCommand:
    @pytest.mark.parametrize("text, tokens", [("200000", 200_000), ("200,000", 200_000), ("300k", 300_000),
                                              ("1.5m", 1_500_000), ("0", 0), ("off", 0), (" 50K ", 50_000)])
    def test_counts_it_understands(self, text, tokens):
        assert parse_token_count(text) == tokens

    @pytest.mark.parametrize("text", ["", "lots", "-5", "1.5", "nank", "infk", "k"])
    def test_counts_it_refuses(self, text):
        assert parse_token_count(text) is None

    def test_it_is_listed(self):
        assert "/limit" in command_names()

    def test_setting_it_saves_it_in_the_users_settings(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=300)
        run_command(app, "/limit 300k")
        assert app.token_limit == 300_000
        assert json.loads((home / ".agentx" / "settings.json").read_text())["tokenLimit"] == 300_000
        assert "300,000 tokens a message" in out.getvalue()
        run_command(app, "/limit")
        assert "Token limit: 300,000" in out.getvalue()
        run_command(app, "/limit off")
        assert app.token_limit == 0 and "Token limit: off" in out.getvalue()

    def test_a_bad_value_shows_the_usage_and_changes_nothing(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=300)
        run_command(app, "/limit lots")
        assert "Usage: /limit" in out.getvalue() and app.token_limit == DEFAULT_TOKEN_LIMIT


class TestSetting:
    def write(self, folder, data):
        (folder / ".agentx").mkdir(parents=True, exist_ok=True)
        (folder / ".agentx" / "settings.json").write_text(json.dumps(data))

    def test_the_users_file_sets_it(self, tmp_path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        self.write(home, {"tokenLimit": 500_000})
        cwd.mkdir()
        assert load_settings(cwd, home).token_limit == 500_000

    def test_a_project_may_lower_it_but_not_raise_or_remove_it(self, tmp_path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        self.write(home, {"tokenLimit": 100_000})
        self.write(cwd, {"tokenLimit": 50_000})
        assert load_settings(cwd, home).token_limit == 50_000
        for value in (900_000, 0):
            self.write(cwd, {"tokenLimit": value})
            settings = load_settings(cwd, home)
            assert settings.token_limit == 100_000 and any("higher token limit" in p for p in settings.problems)

    @pytest.mark.parametrize("value", ["lots", -1, True, 1.5])
    def test_a_bad_value_is_a_problem(self, tmp_path, value):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        self.write(home, {"tokenLimit": value})
        cwd.mkdir()
        settings = load_settings(cwd, home)
        assert settings.token_limit == DEFAULT_TOKEN_LIMIT and any("tokenLimit" in p for p in settings.problems)


class TestCachedInput:
    def test_openai_cached_counts_are_read_from_either_api(self):
        assert _openai_cached_tokens(SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=7))) == 7
        assert _openai_cached_tokens(SimpleNamespace(input_tokens_details=SimpleNamespace(cached_tokens=9))) == 9
        assert _openai_cached_tokens({"input_tokens_details": {"cached_tokens": 4}}) == 4
        assert _openai_cached_tokens(SimpleNamespace(prompt_tokens=5)) == 0
        assert _openai_cached_tokens(MagicMock()) == 0                   # a stand-in adds nothing
        assert _openai_cached_tokens(SimpleNamespace(prompt_tokens_details=None)) == 0

    def test_usage_shows_the_cached_share(self, tmp_path):
        model = MockModel()
        app, out, cwd, home = make_app(tmp_path, Lines(), model, width=300)
        app._track_usage_model("m", model, "chat")
        model._record_usage_counts(input_tokens=1000, output_tokens=5, cache_read_tokens=600)
        run_command(app, "/usage")
        text = out.getvalue()
        assert "600 of the input, 60%, from the provider's cache" in text and "Token limit: 200,000" in text


class TestAStopIsNeverRetried:
    """An error raised while recording usage comes after the reply was paid for: retrying would pay again."""

    def test_an_ordinary_exception_from_the_hook_is_raised_once(self):
        model = MockModel()
        calls = []

        def paid_call():
            calls.append(1)
            model._record_usage_counts(input_tokens=10, output_tokens=5)
            return "reply"

        def stop(_model):
            raise ValueError("too many tokens")
        model.on_usage = stop
        with pytest.raises(ValueError):
            model._with_retry(paid_call, max_retries=3, base_delay=0)
        assert len(calls) == 1

    def test_the_cost_cap_is_raised_once(self):
        from agentx_dev.ChatModel import CostBudgetExceeded
        model = MockModel()
        model.configure_limits(budget_usd=0.0001, input_price_per_1k=1.0, output_price_per_1k=1.0)
        calls = []

        def paid_call():
            calls.append(1)
            model._record_usage_counts(input_tokens=1000, output_tokens=5)
            return "reply"
        with pytest.raises(CostBudgetExceeded):
            model._with_retry(paid_call, max_retries=3, base_delay=0)
        assert len(calls) == 1

    def test_other_errors_are_still_retried(self):
        model = MockModel()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("blip")
            return "ok"
        assert model._with_retry(flaky, max_retries=3, base_delay=0) == "ok" and len(calls) == 3


class TestRetriesAreShownAndTimeoutsRetriedOnce:
    def test_a_timeout_is_retried_once_and_each_retry_is_reported(self):
        model = MockModel()
        seen, calls = [], []
        model.on_retry = lambda attempt, error, delay: seen.append((attempt, type(error).__name__))

        def slow():
            calls.append(1)
            raise TimeoutError("no answer")
        with pytest.raises(TimeoutError):
            model._with_retry(slow, max_retries=3, base_delay=0)
        assert len(calls) == 2 and seen == [(1, "TimeoutError")]

    def test_a_rate_limit_waits_as_asked(self, monkeypatch):
        import time
        model = MockModel()
        waits = []
        monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))

        class RateLimited(Exception):
            status_code = 429
            response = SimpleNamespace(headers={"retry-after": "7"})
        calls = []

        def busy():
            calls.append(1)
            if len(calls) < 2:
                raise RateLimited()
            return "ok"
        assert model._with_retry(busy, max_retries=3, base_delay=0.1) == "ok" and waits == [7.0]

    def test_the_app_prints_a_line_for_a_retry(self, tmp_path):
        model = MockModel()
        app, out, cwd, home = make_app(tmp_path, Lines(), model, width=300)
        app._track_usage_model("m", model, "chat")
        model.on_retry(1, TimeoutError("x"), 0.1)
        assert "A model request got no answer in time; trying again (attempt 2)." in out.getvalue()

    def test_agentx_waits_five_minutes_and_its_client_does_not_retry_by_itself(self):
        from agentx_dev.cli.models import CLIENT_RETRIES, MODEL_TIMEOUT_SEC
        assert MODEL_TIMEOUT_SEC == 300.0 and CLIENT_RETRIES == 0
