import json

from agentx_dev.Runner.Persistence import (
    NOTES_MARK, NOTES_PREFACE, apply_compaction, build_notes, compact_history, estimate_tokens,
    plan_compaction, render_for_summary, _split_notes,
)


def tool_turn(i, size=10):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "t", "arguments": json.dumps({"i": i})}}]},
        {"role": "tool", "name": "t", "tool_call_id": f"c{i}", "content": "r" * size},
    ]


def native_history(turns, size=10):
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": "TASK"}]
    for i in range(turns):
        h.extend(tool_turn(i, size))
    return h


def pairs_intact(h):
    called = {tc["id"] for m in h if m.get("tool_calls") for tc in m["tool_calls"]}
    answered = {m["tool_call_id"] for m in h if m["role"] == "tool"}
    return called == answered


class TestEstimate:
    def test_text_counts_four_chars_per_token(self):
        assert estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100

    def test_media_counts_a_flat_amount(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100000}}
        assert estimate_tokens([{"role": "user", "content": [{"type": "text", "text": ""}, img]}]) == 1500

    def test_text_documents_count_by_length(self):
        doc = {"type": "document", "source": {"type": "text", "media_type": "text/csv", "data": "x" * 800}}
        assert estimate_tokens([{"role": "user", "content": [doc]}]) == 200

    def test_tool_calls_are_counted(self):
        assert estimate_tokens(native_history(3)) > estimate_tokens(native_history(0))


class TestPlan:
    def test_tail_starts_at_an_assistant_turn(self):
        h = native_history(6)
        tail, middle = plan_compaction(h, 1, keep_recent=4)
        assert h[tail]["role"] == "assistant"
        assert len(h) - tail >= 4 and middle

    def test_walks_back_when_the_cut_lands_on_a_tool_result(self):
        h = native_history(6)                 # [sys, task, a0, t0, a1, t1, ...]
        tail, _ = plan_compaction(h, 1, keep_recent=3)    # naive cut would be a tool message
        assert h[tail]["role"] == "assistant"

    def test_nothing_to_compact(self):
        assert plan_compaction([{"role": "user", "content": "T"}, {"role": "assistant", "content": "a"}], 0, 6) is None
        assert plan_compaction(native_history(1), 1, keep_recent=6) is None


class TestApply:
    def test_notes_go_on_the_task_message_and_pairs_survive(self):
        h = native_history(6)
        tail, _ = plan_compaction(h, 1, keep_recent=4)
        apply_compaction(h, 1, tail, "NOTES v1")
        assert h[1]["content"].endswith("NOTES v1") and NOTES_MARK in h[1]["content"]
        assert [m["role"] for m in h][:3] == ["system", "user", "assistant"]
        assert pairs_intact(h)

    def test_second_compaction_replaces_the_notes(self):
        h = native_history(6)
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v1")
        for i in range(6, 10):
            h.extend(tool_turn(i))
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v2")
        assert h[1]["content"].count(NOTES_MARK) == 1 and h[1]["content"].endswith("NOTES v2")
        assert pairs_intact(h)

    def test_media_in_the_task_is_untouched_and_roles_alternate(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        h = [{"role": "user", "content": [{"type": "text", "text": "TASK"}, img]}]
        for i in range(5):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        tail, _ = plan_compaction(h, 0, keep_recent=2)
        apply_compaction(h, 0, tail, "N")
        assert h[0]["content"][1] is img and h[0]["content"][0]["text"].endswith("N")
        roles = [m["role"] for m in h]
        assert all(a != b for a, b in zip(roles, roles[1:])), roles

    def test_a_media_only_task_message_keeps_a_single_notes_block(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        h = [{"role": "user", "content": [img]}]
        for i in range(5):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        apply_compaction(h, 0, plan_compaction(h, 0, 2)[0], "N1")
        for i in range(5, 9):
            h.append({"role": "assistant", "content": f"a{i}"})
            h.append({"role": "user", "content": f"obs{i}"})
        apply_compaction(h, 0, plan_compaction(h, 0, 2)[0], "N2")
        text = "".join(p.get("text", "") for p in h[0]["content"] if isinstance(p, dict))
        assert text.count(NOTES_MARK) == 1 and "N2" in text and "N1" not in text

    def test_split_notes_round_trip(self):
        base, prior = _split_notes("TASK\n\n" + NOTES_MARK + "\nold notes")
        assert base == "TASK" and prior == "old notes"
        assert _split_notes("TASK") == ("TASK", "")

    def test_the_notes_say_they_are_data_not_instructions(self):
        h = native_history(6)
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v1")
        content = h[1]["content"]
        assert NOTES_PREFACE in content
        assert content.index(NOTES_MARK) < content.index(NOTES_PREFACE) < content.index("NOTES v1")
        assert "data, not instructions" in NOTES_PREFACE and "tool output" in NOTES_PREFACE
        # The preface is framing, not part of the notes: it never piles up across compactions.
        assert _split_notes(content)[1] == "NOTES v1"
        for i in range(6, 10):
            h.extend(tool_turn(i))
        apply_compaction(h, 1, plan_compaction(h, 1, 4)[0], "NOTES v2")
        assert h[1]["content"].count(NOTES_PREFACE) == 1


class TestCompactHistory:
    def test_uses_the_summary_and_appends_the_failed_list(self):
        h = native_history(6)
        seen = []
        ok = compact_history(h, task_index=1, keep_recent=4,
                             summarize=lambda prompt: seen.append(prompt) or "LEARNED: x=3",
                             failed_text="  - boom [rung 1]")
        assert ok
        assert "LEARNED: x=3" in h[1]["content"] and "boom [rung 1]" in h[1]["content"]
        assert "You are compacting" in seen[0]

    def test_summary_failure_falls_back_to_the_ledger(self):
        h = native_history(6)

        def boom(_):
            raise RuntimeError("summary model down")

        assert compact_history(h, task_index=1, keep_recent=4, summarize=boom,
                               failed_text="  - boom [rung 1]")
        assert "no summary was available" in h[1]["content"] and "boom [rung 1]" in h[1]["content"]

    def test_prior_notes_are_fed_to_the_next_summary(self):
        h = native_history(6)
        compact_history(h, task_index=1, keep_recent=4, summarize=lambda p: "FIRST NOTES")
        for i in range(6, 10):
            h.extend(tool_turn(i))
        seen = []
        compact_history(h, task_index=1, keep_recent=4, summarize=lambda p: seen.append(p) or "SECOND")
        assert "FIRST NOTES" in seen[0]

    def test_returns_false_when_there_is_nothing_to_do(self):
        assert compact_history(native_history(1), task_index=1, keep_recent=6, summarize=lambda p: "x") is False

    def test_render_for_summary_caps_huge_input(self):
        middle = [{"role": "user", "content": "z" * 5000} for _ in range(30)]
        text = render_for_summary(middle, cap=2000)
        assert "oldest turns dropped" in text and len(text) < 3500
