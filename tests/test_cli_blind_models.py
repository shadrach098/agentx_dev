"""Request-local model-selection boundary; no provider calls."""
import ast
from pathlib import Path

import pytest

from agentx_dev.cli.models import HelperModel
from tests.conftest import MockModel


def roster():
    from agentx_dev.cli.blind_models import BlindModelRoster
    chat = MockModel(script=[])
    chat.model_name = "anthropic/claude-secret"
    extra = MockModel(script=[])
    extra.model_name = "openai/gpt-secret"
    return BlindModelRoster(chat, [HelperModel(
        "deliberate-human-label", extra,
        "OpenAI gpt-secret for careful coding; deliberate-human-label")]), chat, extra


def test_visible_choices_are_opaque_and_comments_are_sanitized():
    choices, chat, extra = roster()
    visible = repr(choices.choices)
    for identity in ("anthropic", "claude", "openai", "gpt", "secret", "deliberate-human-label"):
        assert identity not in visible.lower()
    assert "careful coding" in visible
    assert len(choices.choices) == 2
    assert choices.resolve(choices.choices[0][0]) is chat
    assert choices.resolve(choices.choices[1][0]) is extra


def test_ids_are_request_local_and_invalid_selection_never_defaults():
    first, _, _ = roster()
    second, _, _ = roster()
    assert set(i for i, _ in first.choices).isdisjoint(i for i, _ in second.choices)
    for invalid in ("", "chat", "openai/gpt-secret", first.choices[0][0].upper(), None):
        with pytest.raises(ValueError, match="^Invalid model selection ID\\.$"):
            second.resolve(invalid)


def test_empty_comment_and_deliberate_settings_are_preserved():
    from agentx_dev.cli.blind_models import BlindModelRoster
    chat = MockModel(script=[])
    extra = MockModel(script=[])
    configured = HelperModel("human-choice", extra, "human-choice handles coding")
    choices = BlindModelRoster(chat, [configured])
    assert choices.choices[0][1] == ""
    assert configured.name == "human-choice"
    assert configured.use == "human-choice handles coding"
    assert choices.resolve(choices.choices[1][0]) is extra
    assert "human-choice" not in repr(choices.choices)


def test_roster_does_not_introspect_credentials_or_execute_models():
    source = Path("agentx_dev/cli/blind_models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not names.intersection({"api_key", "token", "environ", "invoke", "ainvoke", "run"})
