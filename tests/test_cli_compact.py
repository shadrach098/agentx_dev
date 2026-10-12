"""Summarising a long conversation without ever dropping messages that have no summary."""

import json

from agentx_dev.cli.compact import (KEEP_USER_MESSAGES, SUMMARY_PROMPT_MARK, SUMMARY_TAG, apply_summary, compact,
                                    history_size, needs_compaction, split_point, summarise, transcript)


def turn(i, size=100, tools=True):
    messages = [{"role": "user", "content": f"question {i} " + "x" * size}]
    if tools:
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_path", "arguments": json.dumps({"path": "a"})}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_path", "content": "result " * 400},
        ]
    messages.append({"role": "assistant", "content": f"answer {i}"})
    return messages


def history(turns, size=100, system=True):
    out = [{"role": "system", "content": "SYSTEM PROMPT"}] if system else []
    for i in range(turns):
        out += turn(i, size)
    return out


def ok_call(prompt):
    return "A good summary of the earlier conversation, long enough."


def tool_calls_are_all_answered(messages):
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    asked = [c["id"] for m in messages for c in (m.get("tool_calls") or [])]
    orphans = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") not in
               {c["id"] for x in messages for c in (x.get("tool_calls") or [])}]
    return all(i in answered for i in asked) and not orphans


def test_size_counts_contents_and_tool_call_arguments():
    h = [{"role": "user", "content": "abcd"}, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "t", "arguments": "12345"}}]}]
    assert history_size(h) == 4 + 5


def test_nothing_happens_below_the_threshold_or_with_too_few_turns():
    assert needs_compaction(history(3, size=100)) is False
    assert needs_compaction(history(KEEP_USER_MESSAGES, size=50_000)) is False      # big, but nothing older to summarise
    assert needs_compaction(history(KEEP_USER_MESSAGES + 1, size=30_000)) is True


def test_the_cut_falls_on_a_user_message_and_keeps_the_last_six_turns():
    h = history(10)
    cut = split_point(h)
    assert h[cut]["role"] == "user" and h[cut]["content"].startswith("question 4 ")
    kept = h[cut:]
    assert sum(1 for m in kept if m["role"] == "user") == KEEP_USER_MESSAGES
    assert tool_calls_are_all_answered(kept)


def test_framework_nudges_are_not_counted_as_turns():
    h = history(8)
    h.insert(5, {"role": "user", "content": "[framework] error: your last response was not valid JSON"})
    cut = split_point(h)
    assert h[cut]["content"].startswith("question ")
    assert sum(1 for m in h[cut:] if m["role"] == "user" and not m["content"].startswith("[framework]")) == 6


def test_the_transcript_cuts_tool_results_and_caps_the_whole_input():
    older = history(3, system=False)
    text = transcript(older)
    assert "User: question 0" in text and "Assistant: answer 0" in text
    assert "Tool read_path result:" in text and len(max(text.splitlines(), key=len)) < 700
    big = transcript(history(200, system=False), limit=5000)
    assert len(big) <= 5100 and big.startswith("(earlier part omitted)")


def test_a_good_summary_replaces_the_older_messages_and_keeps_the_rest():
    h = history(10, size=20_000)
    result = compact(h, ok_call)
    assert result.status == "done" and result.replaced > 0
    new = result.history
    assert new[0] == h[0]                                          # the system message stays first
    assert new[1]["role"] == "user" and new[1]["content"].startswith(SUMMARY_TAG)
    assert new[2]["role"] == "assistant"
    assert new[3:] == h[len(h) - len(new[3:]):]                    # the kept tail is unchanged
    assert tool_calls_are_all_answered(new) and history_size(new) < history_size(h)
    assert result.summary.startswith("A good summary")


def test_the_prompt_asks_for_a_short_summary_of_the_transcript():
    seen = []
    compact(history(10, size=20_000), lambda prompt: seen.append(prompt) or ok_call(prompt))
    assert SUMMARY_PROMPT_MARK in seen[0] and "500 words" in seen[0] and "question 0" in seen[0]


def test_a_failing_empty_or_tiny_summary_leaves_the_history_exactly_as_it_was():
    h = history(10, size=20_000)
    before = json.dumps(h)

    def boom(prompt):
        raise RuntimeError("model down")

    for call in (boom, lambda p: "", lambda p: None, lambda p: "too short"):
        result = compact(h, call)
        assert result.status == "failed" and result.history is h and json.dumps(h) == before


def test_below_the_threshold_compact_reports_none_and_never_calls_the_model():
    def never(prompt):
        raise AssertionError("the model must not be called")

    assert compact(history(3), never).status == "none"


def test_summarise_returns_none_for_a_failure_and_the_text_otherwise():
    assert summarise(history(3, system=False), lambda p: "x" * 100) == "x" * 100
    assert summarise(history(3, system=False), lambda p: "") is None


def test_apply_summary_keeps_every_leading_system_message():
    h = [{"role": "system", "content": "A"}, {"role": "system", "content": "B"}] + history(8, system=False)
    cut = split_point(h)
    new = apply_summary(h, cut, "summary text of some length here")
    assert [m["content"] for m in new[:2]] == ["A", "B"] and new[2]["content"].startswith(SUMMARY_TAG)


def text_mode_turn(i):
    """The runner's text (JSON) mode: no tool_call_id, so the tool result is fed back as a user 'Observation:'."""
    return [
        {"role": "user", "content": f"question {i}"},
        {"role": "assistant", "content": json.dumps({"action": "read_path", "action_input": {"path": "a"}})},
        {"role": "user", "content": "Observation: " + "result " * 400},
        {"role": "assistant", "content": f"answer {i}"},
    ]


def test_text_mode_observations_are_neither_turns_nor_user_text():
    h = [{"role": "system", "content": "S"}]
    for i in range(9):
        h += text_mode_turn(i)
    cut = split_point(h)
    assert h[cut]["content"].startswith("question 3")              # six real questions kept: 3..8
    text = transcript(h[1:cut])
    assert "User: Observation" not in text and "Tool result:" in text
    assert len(max(text.splitlines(), key=len)) < 700
