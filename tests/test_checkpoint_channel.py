"""The checkpoint: the question the framework itself asks when a round is done."""

import asyncio
from contextlib import contextmanager

import pytest

from agentx_dev import Operator as op
from agentx_dev.Operator import (
    CHECKPOINT_QUESTION, CHECKPOINT_SOURCE, NoChannel, OperatorChannel, checkpoint_text, is_stop_reply,
)


class TestStopReply:
    @pytest.mark.parametrize("text", [None, "", "   ", "done", "Done!", " QUIT. ", "stop", "exit", "no",
                                      "No thanks", "no, thanks", "No thank you.", "thanks!", "Thank you.",
                                      "thanks", "that's it", "That's it!", "nope", "all done", "All done.",
                                      "done, thanks", "Done, thank you!", "nothing, thanks", "stop thanks",
                                      "bye", "Goodbye!", "Done?", "no?", "quit?!", "no thanks...",
                                      "Nothing", "That's all", "that’s all."])
    def test_these_end_the_session(self, text):
        assert is_stop_reply(text)

    @pytest.mark.parametrize("text", ["write the emails", "do it again", "not done", "nothing special, check pricing",
                                      "no, tell me more", "thanks, now write the emails", "ok", "okay"])
    def test_these_are_instructions(self, text):
        assert not is_stop_reply(text)


class TestCheckpointText:
    def test_answer_then_outcome_then_the_question(self):
        text = checkpoint_text("Answer body", "Outcome: done.")
        assert text == 'Answer body\n\nOutcome: done.\nWhat should I do next? (blank or "done" to finish)'

    def test_a_long_answer_is_cut_and_says_where_the_rest_is(self):
        text = checkpoint_text("z" * 5000, "Outcome: done.")
        assert text.count("z") == 3000 and "(full text in result.content)" in text

    def test_an_empty_answer_leaves_just_the_outcome_and_question(self):
        assert checkpoint_text("", "Outcome: stuck.").startswith("Outcome: stuck.")

    def test_question_first_puts_the_question_on_top(self):
        text = checkpoint_text("Answer body", "Outcome: done.", question_first=True)
        assert text == ('What should I do next? (blank or "done" to finish)\n\n'
                        'Outcome: done.\n\nAnswer body')

    def test_question_first_still_cuts_a_long_answer(self):
        text = checkpoint_text("z" * 5000, "Outcome: done.", question_first=True)
        assert text.startswith(CHECKPOINT_QUESTION) and text.count("z") == 3000
        assert text.endswith("(full text in result.content)")

    def test_question_first_with_no_answer_is_the_question_and_the_outcome(self):
        assert checkpoint_text("", "Outcome: stuck.", question_first=True) == (
            'What should I do next? (blank or "done" to finish)\n\nOutcome: stuck.')

    def test_the_built_in_terminal_prompt_starts_with_the_question(self, monkeypatch):
        shown = {}

        def fake(text, prefix="", timeout=None):
            shown["text"] = text
            return "go"
        monkeypatch.setattr(op, "builtin_asker", fake)
        built_in = OperatorChannel.create(True)
        assert built_in.builtin is True
        built_in.checkpoint(checkpoint_text("Answer", "Outcome: done.", question_first=built_in.builtin))
        assert shown["text"].startswith("What should I do next?")
        function = OperatorChannel.create(lambda q: shown.update(text=q) or "go")
        assert function.builtin is False
        function.checkpoint(checkpoint_text("Answer", "Outcome: done.", question_first=function.builtin))
        assert shown["text"].endswith('(blank or "done" to finish)')


def channel(asker, **kw):
    return OperatorChannel.create(asker, **kw)


class TestCheckpoint:
    def test_it_asks_and_records_the_reply(self):
        seen = []
        ch = channel(lambda q: seen.append(q) or "write the emails")
        reply = ch.checkpoint("body\n\nOutcome: done.\nWhat should I do next?")
        assert reply.answered and reply.text == "write the emails"
        assert seen == ["body\n\nOutcome: done.\nWhat should I do next?"]
        assert ch.records == [{"source": CHECKPOINT_SOURCE, "question": CHECKPOINT_QUESTION,
                               "answered": True, "reason": None, "deduped": False}]
        events = ch.drain()
        assert [e["type"] for e in events] == ["question", "answer"]
        assert events[0]["source"] == "supervisor" and events[1]["answered"] is True

    def test_it_uses_no_question_slot_and_is_not_an_operator_fact(self):
        ch = channel(lambda q: "go", max_questions=1)
        ch.checkpoint("x")
        ch.checkpoint("y")                     # twice, and not limited, not de-duplicated
        assert ch.asked == 0 and ch.remaining() == 1 and not ch.has_answers()
        assert len(ch.records) == 2

    def test_it_never_touches_memory(self):
        class Boom:
            def __getattr__(self, name):
                raise AssertionError(f"memory.{name} must not be used by the checkpoint")
        ch = channel(lambda q: "go")
        ch.memory = Boom()
        assert ch.checkpoint("x").text == "go"

    def test_the_persistent_deadline_is_paused_while_the_operator_thinks(self):
        class Budget:
            paused_calls = 0

            @contextmanager
            def paused(self):
                Budget.paused_calls += 1
                yield
        ch = channel(lambda q: "go")
        ch.budget = Budget()
        ch.checkpoint("x")
        assert Budget.paused_calls == 1

    @pytest.mark.parametrize("raised,reason", [(NoChannel(), "no_channel"), (RuntimeError("boom"), "error")])
    def test_an_asker_that_cannot_answer_is_a_no_answer(self, raised, reason):
        def asker(q):
            raise raised
        reply = channel(asker).checkpoint("x")
        assert not reply.answered and reply.reason == reason

    def test_a_blank_reply_is_declined(self):
        reply = channel(lambda q: "   ").checkpoint("x")
        assert not reply.answered and reply.reason == "declined"

    def test_the_built_in_prompt_is_labelled_supervisor(self, monkeypatch):
        seen = {}

        def fake(shown, prefix="", timeout=None):
            seen["prefix"] = prefix
            return "go"
        monkeypatch.setattr(op, "builtin_asker", fake)
        OperatorChannel.create(True).checkpoint("x")
        assert seen["prefix"] == "[supervisor]"

    def test_the_async_form_does_the_same(self):
        ch = channel(lambda q: "go", is_async=True)
        reply = asyncio.run(ch.acheckpoint("x"))
        assert reply.text == "go" and ch.records[0]["source"] == "supervisor"
        failed = channel(lambda q: (_ for _ in ()).throw(NoChannel()), is_async=True)
        assert asyncio.run(failed.acheckpoint("x")).reason == "no_channel"


class TestSeedFrom:
    def test_a_later_round_knows_the_answers_and_does_not_ask_them_again(self):
        first = channel(lambda q: "Notion")
        first.ask("Which competitors?", source="planner")
        calls = []
        second = channel(lambda q: calls.append(q) or "should not be asked")
        second.seed_from(first)
        assert second.has_answers() and "Notion" in second.answers_block()
        again = second.ask("Which competitors?", source="planner")
        assert again.text == "Notion" and again.deduped and calls == []

    def test_a_question_nobody_answered_may_be_asked_again(self):
        first = channel(lambda q: None)
        first.ask("Which competitors?", source="planner")
        calls = []
        second = channel(lambda q: calls.append(q) or "Notion")
        second.seed_from(first)
        assert second.ask("Which competitors?", source="planner").text == "Notion" and len(calls) == 1

    def test_the_question_budget_starts_over(self):
        first = channel(lambda q: "a", max_questions=1)
        first.ask("one?", source="planner")
        second = channel(lambda q: "b", max_questions=1)
        second.seed_from(first)
        assert second.remaining() == 1
