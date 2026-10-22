"""The input line colours a slash command as you type it: a known command, a skill, a name that is neither (red),
and a name still being typed. Ordinary messages and paths like /tmp/x are left alone."""

import pytest

from agentx_dev.cli.ui import classify_command, input_style, make_lexer

COMMANDS = ["/help", "/model", "/mode", "/team", "/skills"]
SKILLS = {"/brainstorming": "(skill) Think first"}


@pytest.mark.parametrize("line, kind, end", [
    ("/team brainstorm this repo", "command", 5),
    ("/team", "command", 5),
    ("  /help", "command", 7),
    ("/Model gpt-4o", "command", 6),                # commands are not case-sensitive
    ("/brainstorming add a cache", "skill", 14),
    ("/te", "partial", 3),                          # still typing /team
    ("/mod", "partial", 4),
    ("/frobnicate now", "unknown", 11),
    ("/", "partial", 1),
])
def test_a_slash_name_is_classified(line, kind, end):
    assert classify_command(line, COMMANDS, SKILLS) == (kind, end)


@pytest.mark.parametrize("line", ["fix the bug", "/tmp/build.log fails", "/README.md", "/ is the root", "", "a /team"])
def test_anything_that_is_not_a_command_is_left_alone(line):
    assert classify_command(line, COMMANDS, SKILLS) is None


def fragments(text, extra=None):
    from prompt_toolkit.document import Document
    lexer = make_lexer(COMMANDS, extra if extra is not None else (lambda: SKILLS))
    get_line = lexer.lex_document(Document(text))
    return [get_line(i) for i in range(len(text.split("\n")))]


def test_the_command_name_gets_its_colour_and_the_rest_stays_plain():
    first = fragments("/team brainstorm this repo")[0]
    assert first[0] == ("class:command", "/team")
    assert "".join(t for _, t in first) == "/team brainstorm this repo"
    assert all("command" not in style for style, _ in first[1:])


def test_a_skill_an_unknown_name_and_a_partial_name_get_their_own_colours():
    assert fragments("/brainstorming x")[0][0] == ("class:skill", "/brainstorming")
    assert fragments("/nope x")[0][0] == ("class:unknown-command", "/nope")
    assert fragments("/tea")[0][0] == ("class:partial-command", "/tea")


def test_a_message_and_the_lines_after_the_first_are_plain():
    lines = fragments("/team first line\nsecond /team line")
    assert lines[1] == [("", "second /team line")]
    assert fragments("just a message")[0] == [("", "just a message")]


def test_skills_added_later_are_coloured_at_once_and_a_failing_source_is_harmless():
    live = {}
    assert fragments("/late x", extra=lambda: live)[0][0][0] == "class:unknown-command"
    live["/late"] = "(skill) L"
    assert fragments("/late x", extra=lambda: live)[0][0][0] == "class:skill"

    def broken():
        raise RuntimeError("gone")
    assert fragments("/team x", extra=broken)[0][0] == ("class:command", "/team")


def test_the_style_colours_the_classes_and_the_selected_row_of_the_pop_up():
    rules = dict(input_style().style_rules)
    for name in ("command", "skill", "unknown-command", "partial-command", "completion-menu.completion.current"):
        assert name in rules and rules[name], name
