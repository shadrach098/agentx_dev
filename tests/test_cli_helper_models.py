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


def test_a_model_error_shows_its_message_which_is_written_to_be_shown():
    def factory(name, env):
        raise ModelError("No module named 'anthropic' - install agentx-dev[anthropic]")

    built, notes = build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
    assert built == []
    assert notes == ["Not used: claude-opus-5-5 (No module named 'anthropic' - install agentx-dev[anthropic])"]


def test_a_long_multi_line_model_error_is_one_bounded_line():
    def factory(name, env):
        raise ModelError("first line\n" + "second " * 100)

    _, notes = build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
    assert len(notes) == 1 and "\n" not in notes[0] and "first line second" in notes[0]
    message = notes[0][len("Not used: claude-opus-5-5 ("):-1]
    assert len(message) <= 160 and message.endswith("...")


def test_an_empty_model_error_still_says_what_happened():
    def factory(name, env):
        raise ModelError()

    _, notes = build_team_models(["claude-opus-5-5"], KEYS, factory=factory)
    assert notes == ["Not used: claude-opus-5-5 (could not be set up: ModelError)"]


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


BAD_NAMES = ["claude-" + "x" * 5000, "claude-\x1b[2J\x07 gpt\nbad", "claude opus"]


def _bounded_and_plain(text):
    return len(text) < 300 and not any(ch in text for ch in "\x1b\x07\n\r\t")


def test_a_name_must_be_at_most_100_characters_with_no_spaces_or_control_characters():
    names, _ = clean_team_models(["claude-" + "x" * 93, "claude-" + "x" * 94])
    assert names == ["claude-" + "x" * 93]


def test_long_or_control_character_names_are_skipped_by_clean_with_a_bounded_problem():
    names, problems = clean_team_models(["claude-opus-5-5", *BAD_NAMES])
    assert names == ["claude-opus-5-5"]
    assert len(problems) == len(BAD_NAMES)
    assert all(_bounded_and_plain(p) for p in problems)


def test_bad_names_given_straight_to_usable_are_never_used_and_give_a_bounded_note():
    for bad in BAD_NAMES:
        usable, notes = usable_team_models("claude-sonnet-5-5", [bad], KEYS)
        assert usable == []
        assert len(notes) == 1 and notes[0].startswith("Not used: ") and notes[0].endswith("(not a model name)")
        assert _bounded_and_plain(notes[0])


def test_bad_names_never_reach_the_factory_in_build():
    def factory(name, env):
        raise AssertionError("must not build a name that is not a plain model name")

    for bad in BAD_NAMES:
        built, notes = build_team_models([bad], KEYS, factory=factory)
        assert built == [] and len(notes) == 1 and _bounded_and_plain(notes[0])


def test_the_chat_label_in_the_line_is_shown_bounded_too():
    line = team_models_line("claude-\x1b[2J" + "x" * 5000, [], KEYS)
    assert line.startswith("Helper models: ") and _bounded_and_plain(line)


# -- notes on what a model is for ----------------------------------------------------------------------------

def test_entries_may_carry_a_note_as_name_equals_text_or_as_an_object():
    from agentx_dev.cli.models import parse_team_models
    names, uses, problems = parse_team_models(
        ["gpt-6.1-sol=hard coding and bug fixing", {"model": "gpt-6-luna", "use": "fast reading"}, "gpt-5.5",
         {"model": "gpt-6-astra"}])
    assert names == ["gpt-6.1-sol", "gpt-6-luna", "gpt-5.5", "gpt-6-astra"]
    assert uses == {"gpt-6.1-sol": "hard coding and bug fixing", "gpt-6-luna": "fast reading"}
    assert problems == []


def test_a_bad_note_is_dropped_but_the_model_is_kept():
    from agentx_dev.cli.models import MAX_USE_CHARS, parse_team_models
    names, uses, problems = parse_team_models(
        ["gpt-5.5=" + "x" * (MAX_USE_CHARS + 1), {"model": "gpt-6-luna", "use": "line\x1b[2J"},
         {"model": "gpt-6-astra", "use": 7}])
    assert names == ["gpt-5.5", "gpt-6-luna", "gpt-6-astra"] and uses == {}
    assert len(problems) == 3 and all("\x1b" not in p and len(p) < 300 for p in problems)


def test_an_object_without_a_usable_model_is_skipped():
    from agentx_dev.cli.models import parse_team_models
    names, uses, problems = parse_team_models([{"use": "coding"}, {"model": 5}, {"model": "mistral-x"}])
    assert names == [] and uses == {} and len(problems) == 3


def test_an_empty_note_is_no_note_and_clean_team_models_still_returns_names():
    from agentx_dev.cli.models import parse_team_models
    names, uses, problems = parse_team_models(["gpt-5.5=", "gpt-5.5=again"])
    assert names == ["gpt-5.5"] and uses == {} and problems == []
    assert clean_team_models(["gpt-5.5=coding"]) == (["gpt-5.5"], [])


def test_built_models_carry_their_note():
    built, _ = build_team_models(["claude-opus-5-5"], KEYS, factory=lambda name, env: "M",
                                 uses={"claude-opus-5-5": "hard coding"})
    assert built == [HelperModel("claude-opus-5-5", "M", "hard coding")]


def test_a_name_with_an_equals_sign_is_not_a_model_name():
    from agentx_dev.cli.models import parse_team_models
    names, uses, problems = parse_team_models([{"model": "gpt-5.5=x"}])
    assert names == [] and len(problems) == 1


def test_a_null_note_means_no_note():
    from agentx_dev.cli.models import parse_team_models
    assert parse_team_models([{"model": "gpt-5.5", "use": None}]) == (["gpt-5.5"], {}, [])


# -- notes written by the chat model ---------------------------------------------------------------------------

def test_the_describe_prompt_names_every_model_and_asks_for_json():
    from agentx_dev.cli.models import describe_prompt
    prompt = describe_prompt(["gpt-5.5", "gpt-6-luna"])
    assert "gpt-5.5" in prompt and "gpt-6-luna" in prompt and "JSON" in prompt and "unknown" in prompt


def test_descriptions_are_read_from_a_json_reply_and_marked_auto():
    from agentx_dev.cli.models import AUTO_PREFIX, parse_descriptions
    reply = 'Sure:\n```json\n{"GPT-5.5": "fast, cheaper coding", "gpt-6-luna": "unknown", "other": "x"}\n```'
    assert parse_descriptions(reply, ["gpt-5.5", "gpt-6-luna"]) == {"gpt-5.5": AUTO_PREFIX + "fast, cheaper coding"}


def test_bad_or_hostile_descriptions_are_dropped():
    from agentx_dev.cli.models import parse_descriptions
    reply = '{"a": "x" , "gpt-5.5": "' + "y" * 200 + '", "gpt-6": "line\\u001b[2J", "o3": 7}'
    assert parse_descriptions(reply, ["gpt-5.5", "gpt-6", "o3"]) == {}
    assert parse_descriptions("not json at all", ["gpt-5.5"]) is None          # unreadable, not "unknown"
    assert parse_descriptions(None, ["gpt-5.5"]) is None


def test_an_auto_note_is_told_apart_from_the_users():
    from agentx_dev.cli.models import AUTO_PREFIX, is_auto_note
    assert is_auto_note(AUTO_PREFIX + "x") and not is_auto_note("hard coding") and not is_auto_note("")


def test_text_with_braces_after_the_json_does_not_lose_the_reply():
    from agentx_dev.cli.models import parse_descriptions
    reply = '{"gpt-5.5": "fast"}' + chr(10) + "Also {see notes}"
    assert parse_descriptions(reply, ["gpt-5.5"]) == {"gpt-5.5": "auto: fast"}


def test_a_prefix_is_not_doubled_and_unknown_variants_give_no_note():
    from agentx_dev.cli.models import parse_descriptions
    reply = '{"gpt-5.5": "auto: fast", "gpt-6": "Unknown - newer than my training"}'
    assert parse_descriptions(reply, ["gpt-5.5", "gpt-6"]) == {"gpt-5.5": "auto: fast"}
