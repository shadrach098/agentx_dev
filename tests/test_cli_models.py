"""Choosing GPT or Claude from a flag, settings and the environment."""

import pytest

from agentx_dev.cli import models
from agentx_dev.cli.models import ModelError, choose_model, model_label, provider_for


class Fake:
    def __init__(self, **kw):
        self.kw = kw
        self.model_name = kw.get("model", "default-model")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(models, "Claude", type("Claude", (Fake,), {}))
    monkeypatch.setattr(models, "GPT", type("GPT", (Fake,), {}))


ANT = {"ANTHROPIC_API_KEY": "ak"}
OAI = {"OPENAI_API_KEY": "ok"}


@pytest.mark.parametrize("name,provider", [
    ("claude-sonnet-5-5", "claude"), ("Claude-opus", "claude"), ("gpt-4o", "gpt"), ("GPT-6-sol", "gpt"),
    ("o3", "gpt"), ("o4-mini", "gpt"), ("gemini-2", None), ("ollama", None), ("o", None),
])
def test_provider_for(name, provider):
    assert provider_for(name) == provider


def test_a_named_claude_model(fakes):
    m = choose_model("claude-sonnet-5-5", {**ANT, **OAI})
    assert type(m).__name__ == "Claude" and m.kw == {"model": "claude-sonnet-5-5", "api_key": "ak"}


def test_a_named_gpt_model(fakes):
    m = choose_model("gpt-4o", OAI)
    assert type(m).__name__ == "GPT" and m.kw == {"model": "gpt-4o", "api_key": "ok"}


def test_a_name_whose_provider_has_no_key():
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        choose_model("claude-x", OAI)


def test_an_unknown_provider_name():
    with pytest.raises(ModelError, match="start the name with 'claude' or 'gpt'"):
        choose_model("llama3", {**ANT, **OAI})


def test_no_name_prefers_anthropic_then_openai_and_uses_the_class_default():
    both = choose_model(None, {**ANT, **OAI})
    assert type(both).__name__ == "Claude" and both.kw == {"api_key": "ak"}
    only_oai = choose_model(None, OAI)
    assert type(only_oai).__name__ == "GPT" and only_oai.kw == {"api_key": "ok"}


def test_no_key_at_all_says_what_to_set():
    with pytest.raises(ModelError) as info:
        choose_model(None, {})
    assert "ANTHROPIC_API_KEY" in str(info.value) and "OPENAI_API_KEY" in str(info.value)


def test_a_missing_provider_package_becomes_a_model_error(monkeypatch):
    def boom(**kw):
        raise ImportError("Claude requires the anthropic package")
    monkeypatch.setattr(models, "Claude", boom)
    with pytest.raises(ModelError, match="anthropic package"):
        choose_model("claude-x", ANT)


def test_model_label():
    assert model_label(Fake(model="gpt-4o")) == "gpt-4o"

    class Bare:
        pass
    assert model_label(Bare()) == "Bare"


def test_an_unexpected_constructor_error_never_echoes_its_message(monkeypatch):
    def boom(**kw):
        raise RuntimeError("bad key sk-secret-123")
    monkeypatch.setattr(models, "GPT", boom)
    with pytest.raises(ModelError) as info:
        choose_model("gpt-4o", OAI)
    assert "RuntimeError" in str(info.value) and "sk-secret" not in str(info.value)


def test_model_label_reads_a_gpt_style_defaults_dict():
    class GptLike:
        def __init__(self):
            self.defaults = {"model": "gpt-4o"}
    assert model_label(GptLike()) == "gpt-4o"

    class NoModel:
        defaults = {"temperature": 1}
    assert model_label(NoModel()) == "NoModel"

    class BadDefaults:
        defaults = "not a dict"
    assert model_label(BadDefaults()) == "BadDefaults"


def test_model_label_of_the_real_gpt_class():
    from agentx_dev.ChatModel import GPT
    assert model_label(GPT(model="gpt-4o-mini", api_key="dummy")) == "gpt-4o-mini"


def test_an_unknown_name_is_quoted_not_echoed_raw():
    with pytest.raises(ModelError) as info:
        choose_model("llama\x1b[2J", {**ANT, **OAI})
    assert "\x1b" not in str(info.value) and "llama" in str(info.value)


def test_a_very_long_name_is_capped():
    with pytest.raises(ModelError) as info:
        choose_model("x" * 5000, {**ANT, **OAI})
    assert len(str(info.value)) < 400


@pytest.mark.parametrize("env,expected", [
    ({"ANTHROPIC_API_KEY": "  ", "OPENAI_API_KEY": " ok "}, ("GPT", {"api_key": "ok"})),
    ({"ANTHROPIC_API_KEY": "\t\n", "OPENAI_API_KEY": "ok"}, ("GPT", {"api_key": "ok"})),
])
def test_a_blank_key_counts_as_missing(env, expected):
    m = choose_model(None, env)
    assert (type(m).__name__, m.kw) == expected


def test_a_blank_key_for_a_named_model_is_missing():
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        choose_model("claude-x", {"ANTHROPIC_API_KEY": "   "})
    with pytest.raises(ModelError):
        choose_model(None, {"ANTHROPIC_API_KEY": " ", "OPENAI_API_KEY": ""})
