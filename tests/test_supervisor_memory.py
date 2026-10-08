"""RunMemory: recall blocks, exact-answer lookup, writes, caps, and store failures."""

import asyncio
import threading

import pytest

from agentx_dev import SupervisorMemory as sm
from agentx_dev.Embeddings import HashEmbeddings, VectorStore
from agentx_dev.SupervisorMemory import RunMemory
from tests.memory_helpers import FakeStore, answer_hit, hit, result_hit

HEADER = ("FROM MEMORY (saved from earlier runs; may be out of date, so check anything that "
          "matters with your tools):")
QUESTION = "Which three competitors should I compare?"


def make(store, **kw):
    return RunMemory.create(store, **kw)


class TestValidate:
    def test_none_is_off_and_a_store_is_accepted(self):
        assert sm.validate_memory(None) is None and RunMemory.create(None) is None
        store = FakeStore()
        assert sm.validate_memory(store) is store
        real = VectorStore(embeddings=HashEmbeddings())    # empty, so falsy via __len__
        assert sm.validate_memory(real) is real

    def test_an_object_without_add_and_search_is_a_type_error(self):
        with pytest.raises(TypeError, match="add"):
            sm.validate_memory(object())
        with pytest.raises(TypeError):
            sm.validate_memory("a string")

    def test_ids_are_stable_and_use_the_normalized_text(self):
        assert sm.answer_id("Which  competitors?") == sm.answer_id(" which competitors? ")
        assert sm.answer_id("a").startswith("operator_answer:") and len(sm.answer_id("a")) == len("operator_answer:") + 16
        assert sm.result_id("Compare X") == sm.result_id("compare   x")
        assert sm.result_id("a").startswith("run_result:")


class TestRecall:
    def test_the_block_has_a_header_and_one_labelled_line_per_item(self):
        store = FakeStore([answer_hit(QUESTION, "Notion, Obsidian, Coda"),
                           result_hit("Compare pricing pages", "Asana $10.99"),
                           hit("Our fiscal year starts in April.")])
        block = make(store).recall("compare our competitors", "plan")
        assert block == (
            HEADER + "\n"
            "- [operator answer, 2026-10-05] Q: Which three competitors should I compare? A: Notion, Obsidian, Coda\n"
            "- [earlier result, 2026-10-04] Task: Compare pricing pages Result: Asana $10.99\n"
            "- [note] Our fiscal year starts in April.")

    def test_the_store_is_searched_with_the_configured_limits(self):
        store = FakeStore([hit("x")])
        make(store, top_k=3, min_score=0.5).recall("the query", "plan")
        assert store.searches == [("the query", 3, 0.5)]

    def test_top_k_zero_turns_recall_off(self):
        store = FakeStore([hit("x")])
        assert make(store, top_k=0).recall("q", "plan") == "" and store.searches == []

    def test_no_hits_is_no_block_and_no_event(self):
        rm = make(FakeStore([]))
        assert rm.recall("q", "plan") == "" and rm.drain() == []

    def test_an_item_is_cut_at_600_characters(self):
        block = make(FakeStore([hit("w" * 2000)])).recall("q", "plan")
        line = block.splitlines()[1]
        assert line.startswith("- [note] ") and len(line) == len("- [note] ") + 600 and line.endswith("...")

    def test_the_whole_block_is_cut_at_3000_characters(self):
        block = make(FakeStore([hit(f"{n} " + "w" * 590) for n in range(20)]), top_k=20).recall("q", "plan")
        assert len(block) <= 3000 and 1 <= len(block.splitlines()) - 1 < 20

    def test_an_answer_already_given_in_this_run_is_left_out(self):
        rm = make(FakeStore([answer_hit(QUESTION, "Notion"), hit("another fact")]))
        rm.note_answered("  which three competitors should i compare? ")
        block = rm.recall("q", "plan")
        assert "Which three" not in block and "another fact" in block

    def test_the_same_query_is_searched_once_per_run(self):
        store = FakeStore([hit("x")])
        rm = make(store)
        first = rm.recall("Same  query", "plan")
        assert rm.recall("same query", "step") == first and len(store.searches) == 1

    def test_concurrent_recalls_of_the_same_query_search_once(self):
        import time

        class Slow(FakeStore):
            def search(self, query, *, top_k=5, min_score=0.0):
                time.sleep(0.05)
                return super().search(query, top_k=top_k, min_score=min_score)
        store = Slow([hit("x")])
        rm = make(store)
        threads = [threading.Thread(target=rm.recall, args=("same query", "step")) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(store.searches) == 1

    def test_an_answer_arriving_mid_recall_is_not_served_or_cached_stale(self):
        class Interrupted(FakeStore):
            def search(self, query, *, top_k=5, min_score=0.0):
                result = super().search(query, top_k=top_k, min_score=min_score)
                if len(self.searches) == 1:            # the operator answers while this search runs
                    rm.note_answered(QUESTION)
                return result
        store = Interrupted([answer_hit(QUESTION, "OLD ANSWER"), hit("another fact")])
        rm = make(store)
        first = rm.recall("q", "plan")
        assert "OLD ANSWER" not in first and "another fact" in first
        second = rm.recall("q", "plan")
        assert len(store.searches) == 2                # the first block was not cached
        assert "OLD ANSWER" not in second and "another fact" in second
        assert rm.recall("q", "plan") == second and len(store.searches) == 2

    def test_note_answered_clears_the_cache(self):
        store = FakeStore([hit("x")])
        rm = make(store)
        rm.recall("q", "plan")
        rm.note_answered("anything")
        rm.recall("q", "plan")
        assert len(store.searches) == 2

    def test_a_memory_event_names_the_stage_and_the_hit_count_once(self):
        rm = make(FakeStore([hit("a"), hit("b"), hit("c")]))
        rm.recall("q", "step")
        rm.recall("q", "step")                         # cached: no second event
        assert rm.drain() == [{"type": "memory", "stage": "step", "hits": 3}] and rm.drain() == []

    def test_a_failing_search_is_no_block_not_an_error(self):
        rm = make(FakeStore(boom=True))
        assert rm.recall("q", "plan") == "" and rm.lookup_answer(QUESTION) is None

    def test_a_malformed_hit_is_skipped_not_raised(self):
        class Odd:
            def __init__(self, metadata):
                self.text, self.score, self.id, self.metadata = "odd", 0.9, "o", metadata
        bad = [Odd("not a dict"), Odd({"kind": "operator_answer", "qkey": ["unhashable"]})]
        assert make(FakeStore(bad)).recall("q", "plan") == ""
        rm = make(FakeStore(bad + [hit("a good one")]))
        assert "a good one" in rm.recall("q", "plan")
        assert make(FakeStore(bad)).lookup_answer(QUESTION) is None

    def test_verbose_prints_a_memory_line(self, capsys):
        make(FakeStore([hit("a")]), verbose=True).recall("q", "plan")
        assert "[memory]" in capsys.readouterr().out


class TestLookupAnswer:
    def test_an_exact_question_match_returns_the_stored_answer(self):
        store = FakeStore([hit("noise"), answer_hit(QUESTION, "Notion, Obsidian, Coda")])
        assert make(store).lookup_answer("which three  competitors should i compare?") == "Notion, Obsidian, Coda"
        assert store.searches == [("which three  competitors should i compare?", 5, 0.0)]

    def test_a_huge_stored_answer_is_capped(self):
        store = FakeStore([answer_hit(QUESTION, "x" * 5000)])
        assert make(store).lookup_answer(QUESTION) == "x" * 2000

    def test_a_similar_but_different_question_is_not_reused(self):
        store = FakeStore([answer_hit("Which two competitors should I compare?", "A, B")])
        assert make(store).lookup_answer(QUESTION) is None

    def test_only_operator_answers_count(self):
        store = FakeStore([result_hit(QUESTION, "an old run result")])
        assert make(store).lookup_answer(QUESTION) is None


class TestWrites:
    def test_an_operator_answer_is_stored_under_a_stable_id_with_metadata(self):
        store = FakeStore()
        rm = make(store)
        rm.remember_answer(QUESTION, "Notion, Obsidian, Coda", "planner")
        [(texts, ids, metas)] = store.added
        assert texts == [f"Q: {QUESTION}\nA: Notion, Obsidian, Coda"]
        assert ids == [sm.answer_id(QUESTION)]
        meta = metas[0]
        assert meta["kind"] == "operator_answer" and meta["qkey"] == QUESTION.casefold()
        assert meta["question"] == QUESTION and meta["answer"] == "Notion, Obsidian, Coda"
        assert meta["source"] == "planner" and meta["ts"].endswith("+00:00")
        assert rm.written == [{"kind": "operator_answer", "id": sm.answer_id(QUESTION),
                               "text": f"Q: {QUESTION} A: Notion, Obsidian, Coda"[:80]}]

    def test_the_same_question_reuses_the_id_so_a_new_answer_replaces_the_old(self):
        store = FakeStore()
        rm = make(store)
        rm.remember_answer("Which  competitors?", "A", "planner")
        rm.remember_answer(" which competitors? ", "B", "planner")
        assert store.added[0][1] == store.added[1][1]

    def test_a_run_result_is_stored_cut_at_2000_characters(self):
        store = FakeStore()
        rm = make(store)
        rm.remember_result("Compare pricing", "r" * 5000)
        [(texts, ids, metas)] = store.added
        assert texts[0].startswith("Task: Compare pricing\nResult: ") and len(texts[0].split("Result: ")[1]) == 2000
        assert ids == [sm.result_id("Compare pricing")]
        assert metas[0]["kind"] == "run_result" and metas[0]["task"] == "Compare pricing"
        assert rm.written[0]["kind"] == "run_result" and len(rm.written[0]["text"]) <= 80

    def test_a_non_string_answer_and_a_huge_one_are_stored_as_bounded_strings(self):
        store = FakeStore()
        rm = make(store)
        rm.remember_answer(QUESTION, 42, "planner")
        rm.remember_answer("Another question?", "a" * 5000, "planner")
        first, second = store.added[0][2][0], store.added[1][2][0]
        assert first["answer"] == "42" and isinstance(first["answer"], str)
        assert len(second["answer"]) == 2000 and len(store.added[1][0][0].split("A: ")[1]) == 2000

    def test_a_read_only_memory_writes_nothing(self):
        store = FakeStore()
        rm = make(store, write=False)
        rm.remember_answer(QUESTION, "x", "planner")
        rm.remember_result("task", "result")
        assert store.added == [] and rm.written == []

    def test_a_failing_add_is_logged_and_not_recorded(self):
        rm = make(FakeStore(boom=True))
        rm.remember_answer(QUESTION, "x", "planner")
        rm.remember_result("task", "result")
        assert rm.written == []


class TestWithARealStore:
    def test_an_answer_written_by_one_run_is_found_exactly_by_the_next(self):
        store = VectorStore(embeddings=HashEmbeddings())
        make(store).remember_answer(QUESTION, "Notion, Obsidian, Coda", "planner")
        second = make(store, min_score=0.0)
        assert second.lookup_answer("which three competitors should i compare?") == "Notion, Obsidian, Coda"
        assert "Notion, Obsidian, Coda" in second.recall("anything", "plan")

    def test_a_run_result_comes_back_as_an_earlier_result(self):
        store = VectorStore(embeddings=HashEmbeddings())
        make(store).remember_result("Compare pricing pages", "Asana $10.99")
        block = make(store, min_score=0.0).recall("Compare pricing pages", "plan")
        assert "[earlier result," in block and "Asana $10.99" in block


class TestAsync:
    def test_the_store_is_called_off_the_loop_thread(self):
        store = FakeStore([answer_hit(QUESTION, "Notion"), hit("x")])
        rm = make(store)

        async def go():
            return (await rm.arecall("q", "plan"), await rm.alookup_answer(QUESTION),
                    threading.get_ident())
        block, answer, loop_thread = asyncio.run(go())
        assert "x" in block and answer == "Notion"
        assert store.search_threads and all(t != loop_thread for t in store.search_threads)
