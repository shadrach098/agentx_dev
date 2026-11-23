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
    ("o3", "gpt"), ("o4-mini", "gpt"), ("gemini-2", "local"), ("ollama", "local"), ("o", "local"),
])
def test_provider_for(name, provider):
    assert provider_for(name) == provider


def test_a_named_claude_model(fakes):
    m = choose_model("claude-sonnet-5-5", {**ANT, **OAI})
    assert type(m).__name__ == "Claude" and m.kw == {"model": "claude-sonnet-5-5", "api_key": "ak", "timeout": 300.0, "max_retries": 0,
                                                  "enable_prompt_cache": True, "show_reasoning": True}


def test_a_claude_name_written_with_a_dot_is_the_dashed_id(fakes):
    from agentx_dev.cli.models import claude_model_id
    assert claude_model_id("claude-opus-5.5") == "claude-opus-5-5"
    assert claude_model_id(" claude-sonnet-5.5 ") == "claude-sonnet-5-5"
    assert claude_model_id("claude-opus-5-5") == "claude-opus-5-5"
    assert claude_model_id("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet-20241022"
    assert model_label(choose_model("claude-opus-5.5", ANT)) == "claude-opus-5-5"
    assert choose_model("gpt-5.5", OAI).kw["model"] == "gpt-5.5"           # GPT names keep their dot


def test_a_named_gpt_model(fakes):
    m = choose_model("gpt-4o", OAI)
    assert type(m).__name__ == "GPT" and m.kw == {"model": "gpt-4o", "api_key": "ok", "timeout": 300.0, "max_retries": 0, "show_reasoning": True}


def test_a_name_whose_provider_has_no_key():
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        choose_model("claude-x", OAI)


def test_a_name_with_a_space_is_not_a_model_name():
    with pytest.raises(ModelError, match="not a model name"):
        choose_model("llama 3", {**ANT, **OAI})


def test_no_name_prefers_anthropic_then_openai_and_uses_the_class_default():
    both = choose_model(None, {**ANT, **OAI})
    assert type(both).__name__ == "Claude" and both.kw == {"api_key": "ak", "timeout": 300.0, "max_retries": 0,
                                                     "enable_prompt_cache": True, "show_reasoning": True}
    only_oai = choose_model(None, OAI)
    assert type(only_oai).__name__ == "GPT" and only_oai.kw == {"api_key": "ok", "timeout": 300.0, "max_retries": 0, "show_reasoning": True}


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
    ({"ANTHROPIC_API_KEY": "  ", "OPENAI_API_KEY": " ok "}, ("GPT", {"api_key": "ok", "timeout": 300.0, "max_retries": 0, "show_reasoning": True})),
    ({"ANTHROPIC_API_KEY": "\t\n", "OPENAI_API_KEY": "ok"}, ("GPT", {"api_key": "ok", "timeout": 300.0, "max_retries": 0, "show_reasoning": True})),
])
def test_a_blank_key_counts_as_missing(env, expected):
    m = choose_model(None, env)
    assert (type(m).__name__, m.kw) == expected


def test_a_blank_key_for_a_named_model_is_missing():
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        choose_model("claude-x", {"ANTHROPIC_API_KEY": "   "})
    with pytest.raises(ModelError):
        choose_model(None, {"ANTHROPIC_API_KEY": " ", "OPENAI_API_KEY": ""})


# -- local models -------------------------------------------------------------------------------------------

LOCAL_NAMES = ["tinyllama", "qwen3:8b", "llama3.1:8b-instruct-q4_K_M", "ollama"]


@pytest.mark.parametrize("name", LOCAL_NAMES)
def test_other_names_are_local(name):
    from agentx_dev.cli.models import is_local_name
    assert provider_for(name) == "local" and is_local_name(name)


def test_claude_gpt_unchanged():
    from agentx_dev.cli.models import is_local_name
    for name in ("claude-sonnet-5-5", "gpt-4o", "o3"):
        assert not is_local_name(name)
    assert provider_for("") is None and provider_for("   ") is None


def test_choose_model_local_needs_no_key():
    from agentx_dev.LocalModel import Local
    m = choose_model("qwen3:8b", {}, local_server="http://localhost:9999", local_context=8192)
    assert isinstance(m, Local) and m.base_url == "http://localhost:9999/v1" and m.context_window == 8192
    assert model_label(m) == "qwen3:8b"
    assert choose_model("tinyllama", {}).base_url == "http://localhost:11434/v1"


def test_a_local_name_never_asks_for_a_key():
    from agentx_dev.cli.models import other_provider
    assert other_provider("qwen3:8b", {"OPENAI_API_KEY": "k"}) is None
    assert other_provider("qwen3:8b", {}) is None


def test_provider_name_of_a_local_model():
    from agentx_dev.cli.app import provider_name
    assert provider_name("tinyllama") == "the local server"
    assert provider_name("gpt-4o") == "OpenAI"
