"""OperatorChannel + RunMemory: exact-answer reuse, answer writes, and unchanged shapes without memory."""

import asyncio
import threading

import pytest

from agentx_dev import Operator as op
from agentx_dev import SupervisorMemory as sm
from agentx_dev.Operator import OperatorChannel
from agentx_dev.SupervisorMemory import RunMemory
from tests.memory_helpers import FakeStore, answer_hit, hit

QUESTION = "Which three competitors should I compare?"


def channel(fn=None, store=None, write=True, **kw):
    ch = OperatorChannel.create(fn or (lambda q: pytest.fail("the operator must not be asked")), **kw)
    if store is not None:
        ch.memory = RunMemory(store, write=write, min_score=0.0)
    return ch


class TestExactReuse:
    def test_a_stored_answer_to_the_exact_question_is_used_without_asking(self):
        store = FakeStore([answer_hit(QUESTION, "Notion, Obsidian, Coda")])
        ch = channel(store=store)
        reply = ch.ask("Which  three competitors should I compare?", source="planner")
        assert reply.answered and reply.text == "Notion, Obsidian, Coda" and reply.from_memory
        assert ch.asked == 0                                    # no question slot
        assert ch.records == [{"source": "planner", "question": "Which three competitors should I compare?",
                               "answered": True, "reason": None, "deduped": False, "from_memory": True}]
        events = ch.drain()
        assert [e["type"] for e in events] == ["answer"] and events[0]["from_memory"] is True
        assert "Notion, Obsidian, Coda" in ch.answers_block()
        assert store.added == []                                # not written back

    def test_a_different_question_still_goes_to_the_operator_and_is_written(self):
        store = FakeStore([answer_hit("Which two competitors should I compare?", "A, B")])
        asked = []
        ch = channel(lambda q: asked.append(q) or "Notion, Obsidian, Coda", store=store)
        reply = ch.ask(QUESTION, source="planner")
        assert reply.text == "Notion, Obsidian, Coda" and not reply.from_memory and asked == [QUESTION]
        assert store.kinds_added() == ["operator_answer"] and store.added[0][1] == [sm.answer_id(QUESTION)]
        assert "from_memory" not in ch.records[0] and "from_memory" not in ch.drain()[-1]

    def test_a_repeat_within_the_run_does_not_search_again(self):
        store = FakeStore([answer_hit(QUESTION, "Notion")])
        ch = channel(store=store)
        ch.ask(QUESTION, source="planner")
        again = ch.ask("which three competitors should i compare?", source="helper")
        assert again.answered and again.deduped and len(store.searches) == 1

    def test_reuse_does_not_use_the_agents_one_question(self):
        store = FakeStore([answer_hit(QUESTION, "Notion")])
        asked = []
        ch = channel(lambda q: asked.append(q) or "report.md", store=store)
        assert ch.ask(QUESTION, source="helper").from_memory
        second = ch.ask("Which file?", source="helper")        # the helper's one real question
        assert second.answered and asked == ["Which file?"]

    def test_a_failing_store_falls_back_to_asking(self):
        asked = []
        ch = channel(lambda q: asked.append(q) or "typed", store=FakeStore(boom=True))
        assert ch.ask(QUESTION, source="planner").text == "typed" and asked == [QUESTION]

    def test_a_read_only_memory_still_reuses_but_never_writes(self):
        store = FakeStore([answer_hit(QUESTION, "Notion")])
        ch = channel(lambda q: "typed", store=store, write=False)
        assert ch.ask(QUESTION, source="planner").from_memory
        assert ch.ask("Another question?", source="helper").text == "typed" and store.added == []

    def test_an_operator_answer_is_left_out_of_later_recall_blocks(self):
        store = FakeStore([answer_hit(QUESTION, "Notion")])
        ch = channel(lambda q: "typed", store=store)
        ch.ask("Another question?", source="planner")
        ch.memory.note_answered(QUESTION)
        assert "Which three" not in ch.memory.recall("q", "plan")

    def test_without_memory_nothing_changes(self):
        ch = OperatorChannel.create(lambda q: "typed")
        ch.ask(QUESTION, source="planner")
        assert ch.memory is None and ch.records[0].keys() == {"source", "question", "answered", "reason", "deduped"}
        assert "from_memory" not in ch.drain()[-1]


class TestAsync:
    def test_an_async_ask_reuses_a_stored_answer_off_the_loop(self):
        store = FakeStore([answer_hit(QUESTION, "Notion")])
        ch = channel(store=store, is_async=True)

        async def go():
            return await ch.aask(QUESTION, source="planner"), threading.get_ident()
        reply, loop_thread = asyncio.run(go())
        assert reply.from_memory and reply.text == "Notion" and ch.asked == 0
        assert all(t != loop_thread for t in store.search_threads)

    def test_the_answer_is_written_before_aask_returns(self):
        store = FakeStore()
        ch = channel(lambda q: "typed", store=store, is_async=True)
        asyncio.run(ch.aask(QUESTION, source="planner"))
        assert store.kinds_added() == ["operator_answer"]

    def test_the_write_happens_off_the_loop_and_outside_the_ask_lock(self):
        writes = []

        class Spy(FakeStore):
            def add(self, texts, *, ids=None, metadata=None):
                writes.append((threading.get_ident(), ch._lock.locked()))
                return super().add(texts, ids=ids, metadata=metadata)
        ch = channel(lambda q: "typed", store=Spy(), is_async=True)

        async def go():
            await ch.aask(QUESTION, source="planner")
            return threading.get_ident()
        loop_thread = asyncio.run(go())
        assert writes and writes[0][0] != loop_thread and writes[0][1] is False

    def test_the_sync_write_is_also_outside_the_lock(self):
        seen = []

        class Spy(FakeStore):
            def add(self, texts, *, ids=None, metadata=None):
                seen.append(ch._lock.locked())
                return super().add(texts, ids=ids, metadata=metadata)
        ch = channel(lambda q: "typed", store=Spy())
        ch.ask(QUESTION, source="planner")
        assert seen == [False]
