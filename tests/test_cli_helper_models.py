"""Helper models: cleaning the allowed list, which models are usable, building them."""

from agentx_dev.cli.models import (MAX_TEAM_MODELS, HelperModel, ModelError, build_team_models, clean_team_models,
                                   key_variable, team_models_line, usable_team_models)

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789", "OPENAI_API_KEY": "sk-test-0123456789abcdef"}


def test_a_clean_list_keeps_order_and_drops_repeats_silently():
    names, problems = clean_team_models(["claude-opus-5-5", "gpt-5.5", "Claude-Opus-5-5", " gpt-5.5 "])
    assert names == ["claude-opus-5-5", "gpt-5.5"] and problems == []


def test_unknown_providers_and_non_text_entries_are_skipped_with_a_problem():
    names, problems = clean_team_models(["mistral-large", 7, "", None, "o4-mini"])
    assert names == ["o4-mini"]
    assert len(problems) == 4 and any("mistral-large" in p for p in problems)


def test_more_than_the_cap_keeps_the_first_ones():
    listed = [f"claude-model-{i}" for i in range(MAX_TEAM_MODELS + 2)]
    names, problems = clean_team_models(listed)
    assert names == listed[:MAX_TEAM_MODELS] and any(f"first {MAX_TEAM_MODELS}" in p for p in problems)


def test_a_huge_name_is_cut_in_the_problem_line():
    _, problems = clean_team_models(["x" * 10_000])
    assert len(problems[0]) < 300


def test_key_variable_names_the_provider_key():
    assert key_variable("claude") == "ANTHROPIC_API_KEY" and key_variable("gpt") == "OPENAI_API_KEY"


def test_usable_models_need_a_key_and_leave_out_the_chat_model():
    usable, notes = usable_team_models("claude-sonnet-5-5", ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-5.5"],
                                       {"ANTHROPIC_API_KEY": "sk-ant-test-0123456789"})
    assert usable == ["claude-opus-5-5"]
    assert notes == ["Not used: gpt-5.5 (no OPENAI_API_KEY)"]


def test_the_line_lists_the_chat_model_first_then_the_usable_ones():
    line = team_models_line("claude-sonnet-5-5", ["claude-opus-5-5", "gpt-5.5"], KEYS)
    assert line == "Helper models: claude-sonnet-5-5 (chat), claude-opus-5-5, gpt-5.5"


def test_build_makes_one_model_per_usable_name_with_the_factory():
    made = []

    def factory(name, env):
        made.append(name)
        return f"MODEL<{name}>"

    built, notes = build_team_models(["claude-opus-5-5", "gpt-5.5"], KEYS, chat_label="claude-sonnet-5-5",
                                     factory=factory)
    assert built == [HelperModel("claude-opus-5-5", "MODEL<claude-opus-5-5>"), HelperModel("gpt-5.5", "MODEL<gpt-5.5>")]
    assert made == ["claude-opus-5-5", "gpt-5.5"] and notes == []


def test_a_build_error_is_skipped_and_shows_only_its_type():
    secret = KEYS["OPENAI_API_KEY"]

    def factory(name, env):
        if name.startswith("gpt"):
            raise ValueError(f"bad key {secret}")
        return "ok"

    built, notes = build_team_models(["claude-opus-5-5", "gpt-5.5"], KEYS, factory=factory)
    assert [m.name for m in built] == ["claude-opus-5-5"]
    assert notes == ["Not used: gpt-5.5 (could not be set up: ValueError)"] and secret not in " ".join(notes)


def test_a_model_error_from_the_real_factory_is_skipped_too():
    def factory(name, env):
        raise ModelError("No API key for claude")

    built, notes = build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
    assert built == [] and notes == ["Not used: claude-opus-5-5 (could not be set up: ModelError)"]


def test_a_missing_key_is_noted_and_the_factory_is_not_called():
    def factory(name, env):
        raise AssertionError("must not build without a key")

    built, notes = build_team_models(["gpt-5.5"], {}, factory=factory)
    assert built == [] and notes == ["Not used: gpt-5.5 (no OPENAI_API_KEY)"]


def test_ctrl_c_while_building_is_not_swallowed():
    import pytest

    def factory(name, env):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
