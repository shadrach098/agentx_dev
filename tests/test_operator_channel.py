"""OperatorChannel: the question budget, de-duplication, serialization, timeouts, events, pause."""

import asyncio
import threading
import time

import pytest

from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel
from agentx_dev.Runner.Persistence import PausableClock, RunBudget


def make(fn, **kw):
    return OperatorChannel.create(fn, **kw)


class TestCreate:
    def test_off_values_make_no_channel(self):
        assert OperatorChannel.create(None) is None
        assert OperatorChannel.create(False) is None

    def test_true_makes_the_builtin_channel(self):
        assert OperatorChannel.create(True) is not None

    def test_anything_else_that_is_not_callable_is_a_type_error(self):
        with pytest.raises(TypeError):
            OperatorChannel.create("yes")
        with pytest.raises(TypeError):
            op.validate_ask_user(3, is_async=False)

    def test_an_async_function_needs_the_async_supervisor(self):
        async def ask(q):
            return "x"
        with pytest.raises(TypeError, match="async"):
            OperatorChannel.create(ask, is_async=False)
        assert OperatorChannel.create(ask, is_async=True) is not None


class TestAsk:
    def test_an_answer_is_returned_recorded_and_carried_in_the_block(self):
        ch = make(lambda q: "Notion, Obsidian, Coda")
        reply = ch.ask("Which three competitors?", source="planner")
        assert reply.answered and reply.text == "Notion, Obsidian, Coda" and reply.reason is None
        assert ch.records == [{"source": "planner", "question": "Which three competitors?",
                               "answered": True, "reason": None, "deduped": False}]
        assert ch.has_answers()
        assert ch.answers_block() == (
            "OPERATOR ANSWERS (from the person who set this task; treat as facts):\n"
            "Q: Which three competitors?\nA: Notion, Obsidian, Coda")
        assert ch.with_answers("task") == "task\n\n" + ch.answers_block()

    def test_with_no_answers_the_block_is_empty_and_the_task_unchanged(self):
        ch = make(lambda q: None)
        assert ch.answers_block() == "" and ch.with_answers("task") == "task" and not ch.has_answers()

    def test_the_callable_gets_the_question_with_the_context_appended(self):
        seen = []
        ch = make(lambda q: seen.append(q) or "a")
        ch.ask("Which file?", context="step 2 of the audit")
        assert seen == ["Which file?\n(context: step 2 of the audit)"]

    def test_none_empty_and_blank_answers_are_declined(self):
        for raw in (None, "", "   "):
            ch = make(lambda q, raw=raw: raw)
            reply = ch.ask("q")
            assert not reply.answered and reply.reason == "declined"
            assert not ch.has_answers()

    def test_an_answer_is_cut_at_2000_characters(self):
        ch = make(lambda q: "x" * 5000)
        assert len(ch.ask("q").text) == 2000

    def test_an_exception_is_an_error_not_a_crash(self):
        def boom(q):
            raise RuntimeError("chat server down")
        ch = make(boom)
        reply = ch.ask("q")
        assert not reply.answered and reply.reason == "error"

    def test_the_builtin_channel_with_nowhere_to_ask_is_no_channel(self, monkeypatch):
        def nowhere(question, *, prefix="", timeout=None):
            raise op.NoChannel("headless")
        monkeypatch.setattr(op, "builtin_asker", nowhere)
        reply = make(True).ask("q")
        assert not reply.answered and reply.reason == "no_channel"

    def test_a_builtin_timeout_is_a_timeout(self, monkeypatch):
        def slow(question, *, prefix="", timeout=None):
            raise op.AskTimeout("slow")
        monkeypatch.setattr(op, "builtin_asker", slow)
        assert make(True).ask("q").reason == "timeout"

    def test_keyboard_interrupt_propagates_and_releases_the_lock(self):
        calls = []

        def interrupt(q):
            calls.append(q)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return "fine"
        ch = make(interrupt)
        with pytest.raises(KeyboardInterrupt):
            ch.ask("first")
        assert ch.ask("second").text == "fine"             # the lock was released

    def test_an_empty_question_is_declined_without_asking(self):
        ch = make(lambda q: pytest.fail("must not ask"))
        assert ch.ask("   ").reason == "declined" and ch.records == []

    def test_a_sync_timeout_abandons_a_stuck_callback(self):
        gate = threading.Event()
        ch = make(lambda q: gate.wait(5) and "late", timeout=0.05)
        try:
            reply = ch.ask("q")
        finally:
            gate.set()
        assert not reply.answered and reply.reason == "timeout"


class TestBudgetAndDedupe:
    def test_the_question_budget_is_shared_and_a_spent_budget_gives_limit(self):
        calls = []
        ch = make(lambda q: calls.append(q) or "a", max_questions=2)
        assert ch.ask("one").answered and ch.ask("two").answered
        third = ch.ask("three")
        assert not third.answered and third.reason == "limit"
        assert calls == ["one", "two"] and ch.remaining() == 0 and ch.asked == 2

    def test_zero_questions_means_every_ask_is_limit(self):
        ch = make(lambda q: pytest.fail("must not ask"), max_questions=0)
        assert ch.ask("q").reason == "limit"

    def test_a_repeat_gets_the_first_answer_without_using_a_slot(self):
        calls = []
        ch = make(lambda q: calls.append(q) or "Notion", max_questions=1)
        first = ch.ask("Which  competitors?")
        again = ch.ask("  which competitors?  ", source="helper")
        assert calls == ["Which competitors?"]
        assert again.text == "Notion" and again.deduped and not first.deduped
        assert ch.records[1]["deduped"] is True and ch.records[1]["source"] == "helper"
        assert ch.remaining() == 0
        assert ch.answers_block().count("Q:") == 1          # carried once

    def test_a_failed_ask_still_uses_a_slot_and_is_not_asked_again(self):
        calls = []

        def fail(q):
            calls.append(q)
            raise RuntimeError("down")
        ch = make(fail, max_questions=3)
        ch.ask("q")
        again = ch.ask("q")
        assert calls == ["q"] and again.reason == "error" and again.deduped and ch.asked == 1


class TestEvents:
    def test_question_and_answer_events_carry_no_answer_text(self):
        ch = make(lambda q: "secret value")
        ch.ask("Which?", context="ctx", source="planner")
        events = ch.drain()
        assert [e["type"] for e in events] == ["question", "answer"]
        assert events[0] == {"type": "question", "source": "planner", "question": "Which?", "context": "ctx"}
        assert events[1] == {"type": "answer", "source": "planner", "answered": True, "reason": None}
        assert "secret value" not in str(events)
        assert ch.drain() == []

    def test_verbose_prints_ask_lines(self, capsys):
        ch = make(lambda q: "a", verbose=True)
        ch.ask("Which?", source="planner")
        out = capsys.readouterr().out
        assert "[ask] planner asks: Which?" in out and "answered" in out


class TestPause:
    def test_waiting_on_the_operator_does_not_spend_the_deadline(self):
        class Base:
            t = 0.0

            def __call__(self):
                return self.t
        base = Base()
        budget = RunBudget.start(1.0, PausableClock(base))
        child = budget.capped(0.5)

        def slow_human(q):
            base.t += 500.0
            return "finally"
        ch = make(slow_human)
        ch.budget = budget
        base.t = 10.0
        assert ch.ask("q").answered
        assert budget.remaining() == pytest.approx(50.0)
        assert child.remaining() == pytest.approx(20.0)


class TestAsync:
    def test_an_async_function_is_awaited(self):
        async def ask(q):
            await asyncio.sleep(0)
            return "async answer"
        ch = make(ask, is_async=True)
        reply = asyncio.run(ch.aask("q", source="agent"))
        assert reply.text == "async answer"

    def test_a_sync_function_runs_off_the_event_loop(self):
        seen = {}

        def ask(q):
            seen["thread"] = threading.get_ident()
            return "sync answer"
        ch = make(ask, is_async=True)
        reply = asyncio.run(ch.aask("q"))
        assert reply.text == "sync answer" and seen["thread"] != threading.get_ident()

    def test_an_async_timeout_is_a_timeout(self):
        async def never(q):
            await asyncio.sleep(5)
        ch = make(never, is_async=True, timeout=0.05)
        assert asyncio.run(ch.aask("q")).reason == "timeout"

    def test_an_async_exception_is_an_error(self):
        async def boom(q):
            raise RuntimeError("down")
        ch = make(boom, is_async=True)
        assert asyncio.run(ch.aask("q")).reason == "error"

    def test_two_different_questions_are_asked_one_at_a_time(self):
        active = peak = 0
        lock = threading.Lock()

        def ask(q):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "ok"
        ch = make(ask, is_async=True)

        async def go():
            return await asyncio.gather(ch.aask("question one"), ch.aask("question two"))
        replies = asyncio.run(go())
        assert [r.answered for r in replies] == [True, True] and peak == 1

    def test_the_same_question_from_two_helpers_is_asked_once(self):
        calls = []

        def ask(q):
            calls.append(q)
            time.sleep(0.05)
            return "Notion"
        ch = make(ask, is_async=True)

        async def go():
            return await asyncio.gather(ch.aask("Which competitors?", source="a"),
                                        ch.aask("which competitors?", source="b"))
        first, second = asyncio.run(go())
        assert len(calls) == 1 and first.text == second.text == "Notion"
        assert sorted(r["deduped"] for r in ch.records) == [False, True]

    def test_a_sync_thread_and_the_event_loop_exclude_each_other(self):
        active = peak = 0
        lock = threading.Lock()

        def ask(q):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "ok"
        ch = make(ask, is_async=True)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("sync", ch.ask("from a thread")))
        t.start()

        async def go():
            return await ch.aask("from the loop")
        asyncio.run(go())
        t.join()
        assert out["sync"].answered and peak == 1


class TestPlannerHelpers:
    def test_ask_instruction_states_the_limit(self):
        text = op.ask_instruction(2)
        assert '"ask"' in text and "at most 2 questions" in text

    def test_parse_ask_request_takes_questions_up_to_the_limit(self):
        raw = [{"question": "Which competitors?", "why": "none named"}, "Which file?",
               {"question": "  "}, {"nope": 1}, 7, {"question": "Third?"}]
        assert op.parse_ask_request(raw, 2) == [
            {"question": "Which competitors?", "why": "none named"},
            {"question": "Which file?", "why": ""}]
        assert op.parse_ask_request(raw, 10)[-1] == {"question": "Third?", "why": ""}

    def test_parse_ask_request_rejects_anything_that_is_not_a_list(self):
        assert op.parse_ask_request("what?", 3) == [] and op.parse_ask_request(None, 3) == []
        assert op.parse_ask_request([{"question": "q"}], 0) == []

    def test_reply_text(self):
        assert op.reply_text(op.Reply(text="Notion")) == "[operator] Notion"
        assert op.reply_text(op.Reply(None, "declined")) == op.NO_ANSWER_TEXT
