# Supervisor long-term memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Supervisor` and `AsyncSupervisor` take `memory=<any vector store>`: the planner and every dispatched step automatically get the few relevant stored items as a "FROM MEMORY" block; the Supervisor writes each operator answer and each completed run's final answer; an exact repeat of an answered question is answered from memory.

**Architecture:** A per-run `RunMemory` (new module `agentx_dev/SupervisorMemory.py`) is the only place that talks to the store (recall with a per-run cache, exact-answer lookup, the two kinds of write, events, records). `OperatorChannel` gets a `memory` attribute: exact-answer lookup before a question slot is taken, and answer writes queued and flushed after the ask lock is released. The Supervisors build `RunMemory` per run next to the operator channel and add the block where the operator answers are added today (planning prompts and dispatched steps, never synthesis).

**Tech Stack:** Python 3.10+, existing `VectorStore` contract (`add`, `search`), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-08-supervisor-memory-design.md`

## Global Constraints

- Python 3.10, 3.11, 3.12 must work (CI matrix). No new dependencies.
- With `memory=None` (the default), behavior is identical to today: no new prompt text, no store calls, no new events, `SupervisorResult.memory == []`, and `result.asked` / `answer` events keep their exact current shapes (the new `from_memory` key exists only when true). The existing suite (baseline now: 941 passed, 4 skipped) must stay green.
- Sync and async parity: everything added to `Supervisor` is added to `AsyncSupervisor`; async store calls run in `asyncio.to_thread` so the loop is never blocked.
- A store problem never fails a run: every `search` / `add` call is wrapped; an exception is logged with `logger.warning` and treated as "no hits" / "not written".
- Never add a `Co-Authored-By` trailer or any attribution line to commit messages (the user is the sole contributor). Plain conventional-commit messages.
- Do not push, tag, publish, or touch `pypirc.ini`. Do not install packages. The 3.6.0 release is on hold. Do not change the version in `pyproject.toml`, or the CHANGELOG `[3.6.0]` date/version header.
- No emoji in anything the framework prints or sends to a model. Python code blocks in `docs/` and `README.md` must parse (`ast.parse`).
- Run the full suite from the worktree root with `python -m pytest -q`.
- Exact values from the spec: `memory_top_k` default `4`; `memory_min_score` default `0.2`; `memory_write` default `True`; run-result cut `2000` characters; per-item cut `600`; block cut `3000`; exact-answer lookup `search(question, top_k=5, min_score=0.0)`; kinds `operator_answer` and `run_result`; ids `operator_answer:<16 hex of sha1 of the normalized question>` and `run_result:<16 hex of sha1 of the normalized task>` (normalized = trimmed, spaces collapsed, case-folded, i.e. `Operator._normalize`); block header `FROM MEMORY (saved from earlier runs; may be out of date, so check anything that matters with your tools):`; the planner line `If the FROM MEMORY block already answers a question, do not ask the operator.`; event `{"type": "memory", "stage": "plan" | "step", "hits": <int>}`; `SupervisorResult.memory` entries `{"kind", "id", "text"}` with `text` cut at 80 characters.

## File Structure

- Create `agentx_dev/SupervisorMemory.py`: `validate_memory`, `answer_id`, `result_id`, `RunMemory`, constants.
- Modify `agentx_dev/Operator.py`: `Reply.from_memory`, `OperatorChannel.memory`, lookup in `_begin`, queued writes.
- Modify `agentx_dev/Supervisor.py`: constructor args, per-run memory, planning and step injection, events, `SupervisorResult.memory`, the run-result write (sync and async).
- Tests: `tests/memory_helpers.py` (doubles), `tests/test_supervisor_memory.py`, `tests/test_operator_memory.py`, `tests/test_supervisor_memory_wiring.py`, `tests/test_supervisor_memory_wiring_async.py`.
- Docs/demo: `docs/guides/sub-agents.md`, `docs/cookbook/patterns.md` (pattern 33), `docs/cookbook/faq.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`, `CHANGELOG.md`, `examples/subagents_demo.py`, regenerated `host/data.js`.

---

### Task 1: `RunMemory` and the test doubles

**Files:**
- Create: `agentx_dev/SupervisorMemory.py`
- Create: `tests/memory_helpers.py`
- Test: `tests/test_supervisor_memory.py`

**Interfaces:**
- Consumes: `Operator._normalize`, `Tools.logger`, `Embeddings.VectorHit` / `VectorStore` / `HashEmbeddings` (existing).
- Produces (module `agentx_dev.SupervisorMemory`): constants `MEMORY_RESULT_CHARS=2000`, `MEMORY_ITEM_CHARS=600`, `MEMORY_BLOCK_CHARS=3000`, `KIND_ANSWER`, `KIND_RESULT`, `MEMORY_HEADER`, `MEMORY_ASK_LINE`; `validate_memory(memory) -> memory | None` (`TypeError` when not None and missing callable `add`/`search`); `answer_id(question)`, `result_id(task)`; `RunMemory.create(memory, *, top_k=4, min_score=0.2, write=True, verbose=False) -> Optional[RunMemory]`; methods `recall(query, stage) -> str`, `async arecall(query, stage)`, `lookup_answer(question) -> Optional[str]`, `async alookup_answer(question)`, `note_answered(question)`, `remember_answer(question, answer, source)`, `remember_result(task, answer)`, `drain() -> list`; attributes `written: list[{"kind","id","text"}]`, `write: bool`, `top_k`, `min_score`. `tests/memory_helpers.py` produces `hit`, `answer_hit`, `result_hit`, `FakeStore`.

- [ ] **Step 1: Create the test doubles**

Create `tests/memory_helpers.py`:

```python
"""Test doubles for the Supervisor memory tests: a scripted vector store and hit builders."""

import threading

from agentx_dev.Embeddings import VectorHit


def _qkey(question):
    return " ".join(question.split()).casefold()


def hit(text, kind=None, **meta):
    metadata = dict(meta)
    if kind:
        metadata["kind"] = kind
    return VectorHit(id=f"h{abs(hash(text)) % 10**6}", text=text, score=0.9, metadata=metadata)


def answer_hit(question, answer, ts="2026-10-05T10:00:00+00:00"):
    return VectorHit(id="a", text=f"Q: {question}\nA: {answer}", score=0.9, metadata={
        "kind": "operator_answer", "qkey": _qkey(question), "question": question,
        "answer": answer, "ts": ts})


def result_hit(task, result, ts="2026-10-04T09:00:00+00:00"):
    return VectorHit(id="r", text=f"Task: {task}\nResult: {result}", score=0.9, metadata={
        "kind": "run_result", "task": task, "ts": ts})


class FakeStore:
    """Returns the scripted hits for every search (ignoring the query) and records every call."""

    def __init__(self, hits=(), boom=False):
        self.hits = list(hits)
        self.boom = boom
        self.searches = []          # (query, top_k, min_score)
        self.added = []             # (texts, ids, metadata)
        self.search_threads = []

    def search(self, query, *, top_k=5, min_score=0.0):
        self.searches.append((query, top_k, min_score))
        self.search_threads.append(threading.get_ident())
        if self.boom:
            raise RuntimeError("store down")
        return self.hits[:top_k]

    def add(self, texts, *, ids=None, metadata=None):
        if self.boom:
            raise RuntimeError("store down")
        self.added.append((list(texts), list(ids), list(metadata)))
        return list(ids)

    def kinds_added(self):
        return [meta[0]["kind"] for _t, _i, meta in self.added]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_supervisor_memory.py`:

```python
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
        assert sm.validate_memory(VectorStore(embeddings=HashEmbeddings()))

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

    def test_verbose_prints_a_memory_line(self, capsys):
        make(FakeStore([hit("a")]), verbose=True).recall("q", "plan")
        assert "[memory]" in capsys.readouterr().out


class TestLookupAnswer:
    def test_an_exact_question_match_returns_the_stored_answer(self):
        store = FakeStore([hit("noise"), answer_hit(QUESTION, "Notion, Obsidian, Coda")])
        assert make(store).lookup_answer("which three  competitors should i compare?") == "Notion, Obsidian, Coda"
        assert store.searches == [("which three  competitors should i compare?", 5, 0.0)]

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
```

- [ ] **Step 3: Run to verify it fails**

Run: `python -m pytest tests/test_supervisor_memory.py -q`
Expected: FAIL (`ImportError: cannot import name 'SupervisorMemory'`).

- [ ] **Step 4: Implement**

Create `agentx_dev/SupervisorMemory.py`:

```python
"""
Long-term memory for a Supervisor.

``Supervisor(memory=store)`` gives the planner and every dispatched step the few stored items that
are relevant to their own text, and lets the Supervisor save what it learns (each operator answer,
each completed run's final answer). ``store`` is anything with the vector store contract
(``add``, ``search``): the in-memory ``VectorStore``, ``ChromaVectorStore``, ``QdrantVectorStore``,
``PgVectorStore``. Keeping it persistent is the store's job.

A store problem never fails a run: every call is wrapped and a failure is logged and ignored.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from agentx_dev.Operator import _normalize
from agentx_dev.Tools import logger

MEMORY_RESULT_CHARS = 2000
MEMORY_ITEM_CHARS = 600
MEMORY_BLOCK_CHARS = 3000
KIND_ANSWER = "operator_answer"
KIND_RESULT = "run_result"

MEMORY_HEADER = (
    "FROM MEMORY (saved from earlier runs; may be out of date, so check anything that matters "
    "with your tools):"
)
MEMORY_ASK_LINE = "\n\nIf the FROM MEMORY block already answers a question, do not ask the operator."


def validate_memory(memory: Any) -> Any:
    """Check a ``memory=`` value at construction time and return it unchanged."""
    if memory is None:
        return None
    if not (callable(getattr(memory, "add", None)) and callable(getattr(memory, "search", None))):
        raise TypeError(
            "memory must be a vector store with add() and search() (for example VectorStore, "
            "ChromaVectorStore, QdrantVectorStore or PgVectorStore)"
        )
    return memory


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def answer_id(question: str) -> str:
    """The store id of the operator answer to ``question`` (delete it to be asked again)."""
    return f"{KIND_ANSWER}:{_sha(_normalize(question))}"


def result_id(task: str) -> str:
    return f"{KIND_RESULT}:{_sha(_normalize(task))}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(meta: Dict[str, Any]) -> str:
    kind = meta.get("kind")
    if kind == KIND_ANSWER:
        base = "operator answer"
    elif kind == KIND_RESULT:
        base = "earlier result"
    else:
        return "note"
    day = str(meta.get("ts") or "")[:10]
    return f"{base}, {day}" if day else base


class RunMemory:
    """One per Supervisor run: the only place that talks to the store."""

    def __init__(self, store: Any, *, top_k: int = 4, min_score: float = 0.2,
                 write: bool = True, verbose: bool = False):
        self.store = store
        self.top_k = max(0, int(top_k))
        self.min_score = float(min_score)
        self.write = bool(write)
        self.verbose = verbose
        self.written: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._cache: Dict[str, str] = {}
        self._known: Set[str] = set()                 # normalized questions answered in this run
        self._lock = threading.Lock()                 # state only; never held during a store call
        self._recall_lock = threading.Lock()          # serializes recall so one query is searched once

    @classmethod
    def create(cls, memory: Any, *, top_k: int = 4, min_score: float = 0.2, write: bool = True,
               verbose: bool = False) -> Optional["RunMemory"]:
        validate_memory(memory)
        if memory is None:
            return None
        return cls(memory, top_k=top_k, min_score=min_score, write=write, verbose=verbose)

    # -- events ---------------------------------------------------------------

    def drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            out, self.events = self.events, []
        return out

    def _announce(self, stage: str, hits: int) -> None:
        with self._lock:
            self.events.append({"type": "memory", "stage": stage, "hits": hits})
        if self.verbose:
            print(f"[memory] {stage}: {hits} item(s) from memory")

    # -- reading --------------------------------------------------------------

    def _search(self, query: str, top_k: int, min_score: float) -> List[Any]:
        try:
            return list(self.store.search(query, top_k=top_k, min_score=min_score))
        except Exception as e:                        # a store problem never fails a run
            logger.warning(f"memory search failed; continuing without it: {e}")
            return []

    def _format(self, hits: List[Any]) -> Tuple[str, int]:
        lines: List[str] = []
        used = len(MEMORY_HEADER)
        for h in hits:
            meta = getattr(h, "metadata", None) or {}
            if meta.get("kind") == KIND_ANSWER and meta.get("qkey") in self._known:
                continue                              # already in this run's OPERATOR ANSWERS
            text = " ".join(str(getattr(h, "text", "")).split())
            if len(text) > MEMORY_ITEM_CHARS:
                text = text[: MEMORY_ITEM_CHARS - 3] + "..."
            line = f"- [{_label(meta)}] {text}"
            if used + len(line) + 1 > MEMORY_BLOCK_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return "", 0
        return MEMORY_HEADER + "\n" + "\n".join(lines), len(lines)

    def recall(self, query: str, stage: str) -> str:
        """The FROM MEMORY block for ``query``, or ``""``. Cached per normalized query."""
        key = _normalize(query)
        if self.top_k <= 0 or not key:
            return ""
        with self._recall_lock:                       # parallel steps asking the same thing search once
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
            block, hits = self._format(self._search(query, self.top_k, self.min_score))
            with self._lock:
                self._cache[key] = block
        if block:
            self._announce(stage, hits)
        return block

    async def arecall(self, query: str, stage: str) -> str:
        return await asyncio.to_thread(self.recall, query, stage)

    def lookup_answer(self, question: str) -> Optional[str]:
        """The stored operator answer to exactly this question (after normalization), or None."""
        qkey = _normalize(question)
        for h in self._search(question, 5, 0.0):
            meta = getattr(h, "metadata", None) or {}
            if meta.get("kind") == KIND_ANSWER and meta.get("qkey") == qkey:
                answer = meta.get("answer")
                if isinstance(answer, str) and answer.strip():
                    return answer
        return None

    async def alookup_answer(self, question: str) -> Optional[str]:
        return await asyncio.to_thread(self.lookup_answer, question)

    def note_answered(self, question: str) -> None:
        """The operator answered ``question`` in this run: leave it out of later blocks."""
        with self._lock:
            self._known.add(_normalize(question))
            self._cache.clear()

    # -- writing --------------------------------------------------------------

    def _add(self, doc_id: str, text: str, meta: Dict[str, Any]) -> None:
        try:
            self.store.add([text], ids=[doc_id], metadata=[meta])
        except Exception as e:
            logger.warning(f"memory write failed; continuing: {e}")
            return
        with self._lock:
            self.written.append({"kind": meta["kind"], "id": doc_id,
                                 "text": " ".join(text.split())[:80]})

    def remember_answer(self, question: str, answer: str, source: str) -> None:
        if not self.write:
            return
        self._add(answer_id(question), f"Q: {question}\nA: {answer}", {
            "kind": KIND_ANSWER, "qkey": _normalize(question), "question": question,
            "answer": answer, "source": source, "ts": _now_iso(),
        })

    def remember_result(self, task: str, answer: str) -> None:
        if not self.write:
            return
        self._add(result_id(task), f"Task: {task}\nResult: {str(answer)[:MEMORY_RESULT_CHARS]}", {
            "kind": KIND_RESULT, "task": task, "ts": _now_iso(),
        })
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_supervisor_memory.py -q` then `python -W error -m pytest tests/test_supervisor_memory.py -q`
Expected: all pass, no warnings.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/SupervisorMemory.py tests/memory_helpers.py tests/test_supervisor_memory.py
git commit -m "feat(memory): RunMemory, the one place a Supervisor run talks to its vector store"
```

---

### Task 2: The operator channel uses memory (exact-answer reuse and answer writes)

**Files:**
- Modify: `agentx_dev/Operator.py` (`Reply` ~line 303; `OperatorChannel.__init__` ~line 364; `_begin` ~443; `_finish` ~464; `ask` ~477; `aask` ~488)
- Test: `tests/test_operator_memory.py`

**Interfaces:**
- Consumes (Task 1): `RunMemory.lookup_answer`, `alookup_answer`, `note_answered`, `remember_answer`.
- Produces: `Reply.from_memory: bool = False`; `OperatorChannel.memory` (a `RunMemory` or `None`, set by the Supervisor); records and `answer` events gain `"from_memory": True` only when true; `OperatorChannel._flush_writes()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_memory.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_operator_memory.py -q`
Expected: FAIL (`AttributeError: 'OperatorChannel' object has no attribute 'memory'` / `Reply` has no `from_memory`).

- [ ] **Step 3: Implement** (all in `agentx_dev/Operator.py`)

1. `Reply`: add the field after `deduped`:
   ```python
       from_memory: bool = False             # the answer came from long-term memory, not the operator
   ```
2. `OperatorChannel.__init__`: after `self._lock = ...` lines add:
   ```python
           self.memory: Any = None                       # a RunMemory (set by the Supervisor) or None
           self._pending_writes: List[Tuple[str, str, str]] = []   # (question, answer, source) to store
           self._writes_lock = threading.Lock()
   ```
3. `_begin` becomes (signature gains `stored`):
   ```python
       def _begin(self, question: str, key: str, context: str, source: str,
                  stored: Optional[str] = None) -> Optional[Reply]:
           """Dedupe, memory and budget checks, under the lock. A final reply, or ``None`` when
           the operator must be asked (the slot is taken and the question event is out)."""
           hit = self._seen.get(key)
           if hit is not None:
               return self._finish(source, question, hit, deduped=True)
           if stored is not None:
               reply = Reply(stored, from_memory=True)
               self._seen[key] = reply
               return self._finish(source, question, reply)
           ... (the per-agent, global-limit, slot and question-event code is unchanged)
   ```
4. `_finish` becomes:
   ```python
       def _finish(self, source: str, question: str, reply: Reply, *, deduped: bool = False) -> Reply:
           out = dataclasses.replace(reply, deduped=deduped)
           record = {"source": source, "question": question, "answered": out.answered,
                     "reason": out.reason, "deduped": deduped}
           event = {"type": "answer", "source": source, "answered": out.answered, "reason": out.reason}
           if out.from_memory:                        # present only when true: shapes are unchanged otherwise
               record["from_memory"] = True
               event["from_memory"] = True
           self.records.append(record)
           self.emit(event)
           if out.answered and not deduped:
               self._answers.append((question, out.text))
               if self.memory is not None:
                   self.memory.note_answered(question)
                   if not out.from_memory:
                       with self._writes_lock:
                           self._pending_writes.append((question, out.text, source))
           self._say("answered from memory" if out.from_memory
                     else "answered" if out.answered else f"no answer ({out.reason})")
           return out
   ```
5. Add the helpers and rewrite `ask` / `aask`:
   ```python
       def _stored_answer(self, q: str, key: str) -> Optional[str]:
           """The exact stored answer, or None. Skipped when memory is off or the run already has it."""
           if self.memory is None or key in self._seen:
               return None
           return self.memory.lookup_answer(q)

       def _flush_writes(self) -> None:
           """Store the answers queued by ``_finish``. Called after the ask lock is released, so a
           slow store never holds it."""
           if self.memory is None:
               return
           with self._writes_lock:
               pending, self._pending_writes = self._pending_writes, []
           for question, answer, source in pending:
               self.memory.remember_answer(question, answer, source)

       def ask(self, question: str, context: str = "", source: str = "agent") -> Reply:
           q = " ".join(str(question or "").split())
           if not q:
               return Reply(None, REASON_DECLINED)
           key = _normalize(q)
           with self._lock:
               done = self._begin(q, key, context, source, self._stored_answer(q, key))
               result = done if done is not None else self._end(q, key, source, self._ask_sync(_shown(q, context), source))
           self._flush_writes()
           return result

       async def aask(self, question: str, context: str = "", source: str = "agent") -> Reply:
           q = " ".join(str(question or "").split())
           if not q:
               return Reply(None, REASON_DECLINED)
           key = _normalize(q)
           while not self._lock.acquire(blocking=False):      # never blocks the loop; a cancel cannot leak the lock
               await asyncio.sleep(0.02)
           try:
               stored = None
               if self.memory is not None and key not in self._seen:
                   stored = await self.memory.alookup_answer(q)
               done = self._begin(q, key, context, source, stored)
               result = done if done is not None else self._end(q, key, source, await self._ask_async(_shown(q, context), source))
           finally:
               self._lock.release()
           if self._pending_writes:
               await asyncio.to_thread(self._flush_writes)
           return result
   ```
   (Keep the surrounding docstrings; do not change `_end`, `_ask_sync`, `_ask_async`.)

- [ ] **Step 4: Run to verify it passes, then the operator suites**

Run: `python -m pytest tests/test_operator_memory.py -q` then `python -m pytest tests/test_operator_channel.py tests/test_operator_asker.py tests/test_operator_tool.py tests/test_operator_guidance.py -q`
Expected: all pass (the existing channel tests are unaffected: with `memory=None` nothing new runs).

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Operator.py tests/test_operator_memory.py
git commit -m "feat(operator): answer an exact repeat from memory and queue answer writes after the ask lock"
```

---

### Task 3: Wire the sync Supervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py` (imports ~line 40; `SupervisorResult` ~164; `_SpawnMixin` ~898; `Supervisor.__init__` ~1000-1115; `_plan_once` ~1145; `_run_plan` dispatch ~1517; `stream` ~1538)
- Test: `tests/test_supervisor_memory_wiring.py`

**Interfaces:**
- Consumes (Tasks 1-2): `RunMemory`, `validate_memory`, `MEMORY_ASK_LINE`, `OperatorChannel.memory`.
- Produces: `Supervisor(..., memory=None, memory_top_k=4, memory_min_score=0.2, memory_write=True)`; `SupervisorResult.memory: List[dict]`; `_SpawnMixin` helpers `_new_run_memory()`, `_memory_block(query, stage)`, `_planning_task(user_task)`, `_step_context(dispatched_query, step_query)`, `_memory_records()`, and `_drain_operator_events` also draining memory events; instance attrs `memory`, `memory_top_k`, `memory_min_score`, `memory_write`, `_run_memory`. Task 4 mirrors them on `AsyncSupervisor` with async variants.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_memory_wiring.py`:

```python
"""Supervisor(memory=...): the planner and every step get relevant memory; answers and results are saved."""

import json

import pytest

from agentx_dev import Persistence, Supervisor
from agentx_dev import SupervisorMemory as sm
from agentx_dev.Embeddings import HashEmbeddings, VectorStore
from tests.memory_helpers import FakeStore, answer_hit, hit, result_hit
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})
QUESTION = "Which three competitors should I compare?"
ANSWER = "Notion, Obsidian, Coda"
FACT = "Our fiscal year starts in April."
MARK = "FROM MEMORY"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return Supervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def synth_prompt(model):
    return next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))


class TestRead:
    def test_the_planner_sees_the_block_and_synthesis_does_not(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = supervisor(model, memory=store).run("Compare our competitors")
        assert MARK in model.planner_prompts()[0] and FACT in model.planner_prompts()[0]
        assert MARK not in synth_prompt(model)
        assert result.query == "Compare our competitors"
        assert store.searches[0][0] == "Compare our competitors"

    def test_every_dispatched_step_gets_a_block_for_its_own_query(self):
        model = router(plans=[plan_json(step("a", "worker", "look up A"), step("b", "worker", "look up B", deps=["a"]))])
        worker = ScriptedRunner()
        store = FakeStore([hit(FACT)])
        supervisor(model, worker, memory=store).run("task")
        assert all(MARK in call[0] and FACT in call[0] for call in worker.calls) and len(worker.calls) == 2
        assert [s[0] for s in store.searches] == ["task", "look up A", "look up B"]

    def test_a_retry_carries_the_block(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("ok", "done"))
        supervisor(model, worker, memory=FakeStore([hit(FACT)])).run("task")
        assert len(worker.calls) == 2 and all(MARK in call[0] for call in worker.calls)

    def test_a_recovery_round_gets_the_planner_block(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        supervisor(model, worker, memory=FakeStore([hit(FACT)]), max_subtask_retries=0,
                   persistence=Persistence(max_minutes=5, max_replans=1)).run("task")
        prompts = model.planner_prompts()
        assert len(prompts) == 2 and all(MARK in p for p in prompts)

    def test_the_planner_is_told_not_to_ask_what_memory_answers(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, memory=FakeStore([hit(FACT)]), ask_user=lambda q: "x").run("task")
        assert sm.MEMORY_ASK_LINE.strip() in model.planner_prompts()[0]

    def test_no_hits_means_no_block_and_no_ask_line(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, memory=FakeStore([]), ask_user=lambda q: "x").run("task")
        prompt = model.planner_prompts()[0]
        assert MARK not in prompt and sm.MEMORY_ASK_LINE.strip() not in prompt

    def test_memory_events_come_before_the_plan_and_after_each_step(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        events = list(supervisor(model, memory=FakeStore([hit(FACT)])).stream("task"))
        kinds = [e["type"] for e in events]
        mem = [e for e in events if e["type"] == "memory"]
        assert [(e["stage"], e["hits"]) for e in mem] == [("plan", 1), ("step", 1)]
        assert kinds.index("memory") < kinds.index("plan")

    def test_top_k_zero_never_searches_but_still_writes(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        supervisor(model, memory=store, memory_top_k=0).run("task")
        assert store.searches == [] and store.kinds_added() == ["run_result"]

    def test_the_configured_limits_reach_the_store(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        supervisor(model, memory=store, memory_top_k=2, memory_min_score=0.7).run("task")
        assert all(s[1:] == (2, 0.7) for s in store.searches)


class TestWrite:
    def test_a_completed_run_saves_its_final_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"))], synth="The final answer.")
        store = FakeStore()
        result = supervisor(model, memory=store).run("Compare our competitors")
        [(texts, ids, metas)] = store.added
        assert texts == ["Task: Compare our competitors\nResult: The final answer."]
        assert ids == [sm.result_id("Compare our competitors")] and metas[0]["kind"] == "run_result"
        assert [m["kind"] for m in result.memory] == ["run_result"] and result.memory[0]["id"] == ids[0]

    def test_a_run_that_did_not_finish_saves_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = supervisor(model, ScriptedRunner(("", "stuck")), memory=store, max_subtask_retries=0).run("task")
        assert result.outcome != "done" and store.added == [] and result.memory == []

    def test_a_no_plan_run_saves_nothing(self):
        store = FakeStore()
        result = supervisor(router(plans=["not json"]), memory=store).run("task")
        assert result.outcome == "stuck" and store.added == []

    def test_a_read_only_memory_writes_nothing_but_still_reads(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = supervisor(model, memory=store, memory_write=False).run("task")
        assert store.added == [] and result.memory == [] and MARK in model.planner_prompts()[0]

    def test_an_operator_answer_is_saved_when_it_is_given(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = supervisor(model, memory=store, ask_user=lambda q: ANSWER).run("Compare our competitors")
        assert store.kinds_added() == ["operator_answer", "run_result"]
        assert store.added[0][1] == [sm.answer_id(QUESTION)]
        assert [m["kind"] for m in result.memory] == ["operator_answer", "run_result"]


class TestExactReuse:
    def test_a_stored_answer_is_used_instead_of_asking(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore([answer_hit(QUESTION, ANSWER)])
        result = supervisor(model, memory=store, ask_user=lambda q: pytest.fail("asked the operator")).run("task")
        assert result.asked[0]["from_memory"] is True and result.asked[0]["answered"] is True
        assert ANSWER in model.planner_prompts()[1] and "operator_answer" not in store.kinds_added()

    def test_a_second_run_with_a_real_store_does_not_ask_again(self):
        store = VectorStore(embeddings=HashEmbeddings())
        asked = []
        first = supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                           memory_min_score=0.0, ask_user=lambda q: asked.append(q) or ANSWER)
        first.run("Compare our competitors")
        assert len(asked) == 1 and len(store) == 2
        second = supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                            memory_min_score=0.0, ask_user=lambda q: pytest.fail("asked again"))
        result = second.run("Compare our competitors")
        assert result.asked[0]["from_memory"] is True


class TestRobustness:
    def test_a_failing_store_does_not_fail_the_run(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = supervisor(model, memory=FakeStore(boom=True)).run("task")
        assert result.outcome == "done" and result.memory == []

    def test_a_bad_memory_value_is_a_type_error_and_bad_limits_are_value_errors(self):
        with pytest.raises(TypeError):
            supervisor(router(), memory=object())
        with pytest.raises(ValueError):
            supervisor(router(), memory=FakeStore(), memory_top_k=-1)

    def test_nothing_changes_without_memory(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = supervisor(model, worker).run("task")
        assert MARK not in model.planner_prompts()[0] and MARK not in worker.calls[0][0]
        assert result.memory == [] and not any(e["type"] == "memory" for e in
                                               supervisor(router(plans=[plan_json(step("s1", "worker"))])).stream("task"))

    def test_a_second_run_on_the_same_supervisor_starts_with_fresh_records(self):
        store = FakeStore()
        sup = supervisor(router(plans=[plan_json(step("s1", "worker")), plan_json(step("s1", "worker"))]), memory=store)
        first = sup.run("task one")
        second = sup.run("task two")
        assert len(first.memory) == 1 and len(second.memory) == 1 and first.memory[0]["id"] != second.memory[0]["id"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_supervisor_memory_wiring.py -q`
Expected: FAIL (`TypeError: Supervisor.__init__() got an unexpected keyword argument 'memory'`).

- [ ] **Step 3: Implement** (all in `agentx_dev/Supervisor.py`; locate code by the descriptions, line numbers are approximate)

1. Imports: next to the `from agentx_dev.Operator import (...)` block add
   ```python
   from agentx_dev.SupervisorMemory import MEMORY_ASK_LINE, RunMemory, validate_memory
   ```
2. `SupervisorResult`: after the `asked` field add
   ```python
       # What this run saved to long-term memory (3.6): kind, id, text (first 80 characters).
       # Empty when memory is off, read-only, or nothing qualified.
       memory: List[Dict[str, Any]] = Field(default_factory=list)
   ```
3. `_SpawnMixin`: extend the docstring needs with `self.memory`, `self.memory_top_k`, `self.memory_min_score`, `self.memory_write`, `self._run_memory`; add the class attribute `_run_memory: Optional[RunMemory] = None` next to `_operator`; make `_drain_operator_events` drain both:
   ```python
       def _drain_operator_events(self):
           if self._operator is not None:
               yield from self._operator.drain()
           if self._run_memory is not None:
               yield from self._run_memory.drain()
   ```
   and add (after `_asked_records`):
   ```python
       def _new_run_memory(self) -> Optional[RunMemory]:
           return RunMemory.create(self.memory, top_k=self.memory_top_k, min_score=self.memory_min_score,
                                   write=self.memory_write, verbose=self.verbose)

       def _memory_block(self, query: str, stage: str) -> str:
           """The FROM MEMORY block for ``query`` ("" when memory is off or nothing matches)."""
           return self._run_memory.recall(query, stage) if self._run_memory is not None else ""

       def _planning_task(self, user_task: str) -> str:
           """The task as a PLANNING prompt sees it: the original, any operator answers, and the
           memory block for the task. Synthesis keeps ``_task_for_model`` (no memory there)."""
           task = self._task_for_model(user_task)
           block = self._memory_block(user_task, "plan")
           return f"{task}\n\n{block}" if block else task

       def _step_context(self, dispatched_query: str, step_query: str) -> str:
           """A dispatched step's query with the operator answers and the memory block for the
           step's own query."""
           query = self._with_answers(dispatched_query)
           block = self._memory_block(step_query, "step")
           return f"{query}\n\n{block}" if block else query

       def _memory_records(self) -> List[Dict[str, Any]]:
           return list(self._run_memory.written) if self._run_memory is not None else []
   ```
4. `Supervisor.__init__`: add parameters `memory: Any = None, memory_top_k: int = 4, memory_min_score: float = 0.2, memory_write: bool = True` at the end of the signature; add to the docstring Args:
   ```
               memory: (3.6) Long-term memory: any vector store with ``add`` and ``search``
                   (``VectorStore``, ``ChromaVectorStore``, ``QdrantVectorStore``,
                   ``PgVectorStore``). The planner and every dispatched step get the few
                   relevant stored items as a FROM MEMORY block; the Supervisor saves each
                   operator answer and each completed run's final answer; an exact repeat of an
                   answered question is answered from memory. ``None`` (default) is off.
               memory_top_k: Items per lookup (default 4; 0 turns recall off).
               memory_min_score: Matches below this score are dropped (default 0.2; about 0.5
                   for OpenAI embeddings, about 0.1 for HashEmbeddings).
               memory_write: False makes the store read-only.
   ```
   and in the body, after `self._operator: Optional[OperatorChannel] = None`:
   ```python
           self.memory = validate_memory(memory)
           if int(memory_top_k) < 0:
               raise ValueError("memory_top_k must be >= 0")
           self.memory_top_k = int(memory_top_k)
           self.memory_min_score = float(memory_min_score)
           self.memory_write = bool(memory_write)
           self._run_memory: Optional[RunMemory] = None
   ```
5. `Supervisor._plan_once`: use `user_task=self._planning_task(user_task)` in the `.format(...)` call, and change the ask block to
   ```python
           if offer:
               prompt = prompt + ask_instruction(self._operator.remaining())
               if self._memory_block(user_task, "plan"):
                   prompt = prompt + MEMORY_ASK_LINE
   ```
6. `_run_plan` (sync): replace `dispatched_query = self._with_answers(dispatched_query)` with `dispatched_query = self._step_context(dispatched_query, sub_query)`.
7. `stream()`:
   - Event shapes docstring: add `- {"type": "memory", "stage": "plan" | "step", "hits": int}`.
   - After the operator-channel lines (`self._operator = ...` through `self._operator.budget = budget`) add:
     ```python
           self._run_memory = self._new_run_memory()
           if self._operator is not None:
               self._operator.memory = self._run_memory
     ```
   - In the stopped-before-planning branch, after `stopped.asked = self._asked_records()` add `stopped.memory = self._memory_records()`; in the no-plan `SupervisorResult(...)` add `memory=self._memory_records(),`.
   - Replace the final block (from `final = self._synthesize(...)` through the `SupervisorResult(...)` construction) so the outcome is computed once and the run result is saved when it is `done`:
     ```python
           final = self._synthesize(user_task, shown, unresolved=unresolved)
           if self.verbose:
               _log_final(final)

           outcome = _supervisor_outcome(subtask_results, budget_reason)
           if self._run_memory is not None and outcome == OUTCOME_DONE:
               self._run_memory.remember_result(user_task, final)

           result = SupervisorResult(
               query=user_task, content=final,
               subtasks=subtask_results, plan=plan,
               outcome=outcome,
               spawned=list(self._spawn_policy().run.records),
               asked=self._asked_records(),
               memory=self._memory_records(),
           )
     ```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_supervisor_memory_wiring.py -q` then `python -m pytest -q`
Expected: new tests pass; full suite green (941 + new). If an existing test fails only because it asserts an exact `SupervisorResult` dict or event list, report it instead of loosening production behavior.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_memory_wiring.py
git commit -m "feat(supervisor): memory= gives the planner and every step relevant memory and saves answers and results (sync)"
```

---

### Task 4: Wire the AsyncSupervisor

**Files:**
- Modify: `agentx_dev/Supervisor.py` (`_SpawnMixin` additions; `AsyncSupervisor.__init__`; `_plan_once`; `_run_subtask`; `astream`)
- Test: `tests/test_supervisor_memory_wiring_async.py`

**Interfaces:**
- Consumes: everything Task 3 produced, plus `RunMemory.arecall` / `alookup_answer`.
- Produces: `AsyncSupervisor(..., memory=None, memory_top_k=4, memory_min_score=0.2, memory_write=True)` with the same behavior; `_SpawnMixin` async variants `_amemory_block`, `_aplanning_task`, `_astep_context`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_memory_wiring_async.py`:

```python
"""AsyncSupervisor(memory=...): the same behaviour as the sync class, with store calls off the loop."""

import asyncio
import json
import threading

import pytest

from agentx_dev import AsyncSupervisor
from agentx_dev import SupervisorMemory as sm
from agentx_dev.Embeddings import HashEmbeddings, VectorStore
from tests.memory_helpers import FakeStore, answer_hit, hit
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?", "why": "none named"}]})
QUESTION = "Which three competitors should I compare?"
ANSWER = "Notion, Obsidian, Coda"
FACT = "Our fiscal year starts in April."
MARK = "FROM MEMORY"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


async def collect(sup, task="task"):
    return [e async for e in sup.astream(task)]


def synth_prompt(model):
    return next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))


class TestRead:
    def test_the_planner_sees_the_block_and_synthesis_does_not(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = run(supervisor(model, memory=store), "Compare our competitors")
        assert MARK in model.planner_prompts()[0] and MARK not in synth_prompt(model)
        assert result.query == "Compare our competitors"

    def test_every_step_gets_a_block_and_the_store_is_called_off_the_loop(self):
        model = router(plans=[plan_json(step("a", "worker", "look up A"), step("b", "worker", "look up B"))])
        worker = ScriptedRunner()
        store = FakeStore([hit(FACT)])
        loop_thread = {}

        async def go():
            loop_thread["id"] = threading.get_ident()
            return await supervisor(model, worker, memory=store).run("task")
        asyncio.run(go())
        assert all(MARK in call[0] for call in worker.calls) and len(worker.calls) == 2
        assert {s[0] for s in store.searches} == {"task", "look up A", "look up B"}
        assert all(t != loop_thread["id"] for t in store.search_threads)

    def test_parallel_steps_with_the_same_query_search_once(self):
        model = router(plans=[plan_json(step("a", "worker", "same query"), step("b", "worker", "same query"))])
        store = FakeStore([hit(FACT)])
        run(supervisor(model, memory=store))
        assert [s[0] for s in store.searches].count("same query") == 1

    def test_the_planner_is_told_not_to_ask_what_memory_answers(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        run(supervisor(model, memory=FakeStore([hit(FACT)]), ask_user=lambda q: "x"))
        assert sm.MEMORY_ASK_LINE.strip() in model.planner_prompts()[0]

    def test_events_and_top_k_zero(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        events = asyncio.run(collect(supervisor(model, memory=FakeStore([hit(FACT)]))))
        mem = [e for e in events if e["type"] == "memory"]
        assert [(e["stage"], e["hits"]) for e in mem] == [("plan", 1), ("step", 1)]
        assert [e["type"] for e in events].index("memory") < [e["type"] for e in events].index("plan")
        store = FakeStore([hit(FACT)])
        run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=store, memory_top_k=0))
        assert store.searches == [] and store.kinds_added() == ["run_result"]


class TestWrite:
    def test_a_completed_run_saves_its_final_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"))], synth="The final answer.")
        store = FakeStore()
        result = run(supervisor(model, memory=store), "Compare our competitors")
        assert store.added[0][0] == ["Task: Compare our competitors\nResult: The final answer."]
        assert [m["kind"] for m in result.memory] == ["run_result"]

    def test_an_unfinished_run_and_a_read_only_memory_save_nothing(self):
        store = FakeStore()
        result = run(supervisor(router(plans=[plan_json(step("s1", "worker"))]),
                                ScriptedRunner(("", "stuck")), memory=store, max_subtask_retries=0))
        assert result.outcome != "done" and store.added == []
        store2 = FakeStore()
        run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=store2, memory_write=False))
        assert store2.added == []

    def test_an_operator_answer_is_saved_with_an_async_callback(self):
        async def ask(q):
            return ANSWER
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = run(supervisor(model, memory=store, ask_user=ask))
        assert store.kinds_added() == ["operator_answer", "run_result"]
        assert [m["kind"] for m in result.memory] == ["operator_answer", "run_result"]


class TestExactReuse:
    def test_a_stored_answer_is_used_instead_of_asking(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore([answer_hit(QUESTION, ANSWER)])
        result = run(supervisor(model, memory=store, ask_user=lambda q: pytest.fail("asked the operator")))
        assert result.asked[0]["from_memory"] is True and ANSWER in model.planner_prompts()[1]

    def test_a_second_run_with_a_real_store_does_not_ask_again(self):
        store = VectorStore(embeddings=HashEmbeddings())
        asked = []
        run(supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                       memory_min_score=0.0, ask_user=lambda q: asked.append(q) or ANSWER))
        assert len(asked) == 1 and len(store) == 2
        result = run(supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                                memory_min_score=0.0, ask_user=lambda q: pytest.fail("asked again")))
        assert result.asked[0]["from_memory"] is True


class TestRobustness:
    def test_a_failing_store_does_not_fail_the_run(self):
        result = run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=FakeStore(boom=True)))
        assert result.outcome == "done" and result.memory == []

    def test_validation(self):
        with pytest.raises(TypeError):
            supervisor(router(), memory=object())
        with pytest.raises(ValueError):
            supervisor(router(), memory=FakeStore(), memory_top_k=-1)

    def test_nothing_changes_without_memory(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = run(supervisor(model, worker))
        assert MARK not in model.planner_prompts()[0] and MARK not in worker.calls[0][0] and result.memory == []

    def test_a_planning_time_budget_stop_still_reports_what_was_written(self):
        from agentx_dev import CostBudgetExceeded, Persistence
        from tests.conftest import MockModel
        from tests.subagent_helpers import PLANNER_MARK

        planner_calls = []

        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                planner_calls.append(1)
                if len(planner_calls) == 1:
                    return ASK_PLAN
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "unused"
        store = FakeStore()
        sup = supervisor(MockModel(script=script), memory=store, ask_user=lambda q: ANSWER,
                         persistence=Persistence(max_minutes=5))
        result = run(sup, "Compare our competitors")
        assert result.outcome == "out_of_budget"
        assert [m["kind"] for m in result.memory] == ["operator_answer"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_supervisor_memory_wiring_async.py -q`
Expected: FAIL (`TypeError ... unexpected keyword argument 'memory'`).

- [ ] **Step 3: Implement** (all in `agentx_dev/Supervisor.py`)

1. `_SpawnMixin`: add the async variants next to the sync ones:
   ```python
       async def _amemory_block(self, query: str, stage: str) -> str:
           return await self._run_memory.arecall(query, stage) if self._run_memory is not None else ""

       async def _aplanning_task(self, user_task: str) -> str:
           task = self._task_for_model(user_task)
           block = await self._amemory_block(user_task, "plan")
           return f"{task}\n\n{block}" if block else task

       async def _astep_context(self, dispatched_query: str, step_query: str) -> str:
           query = self._with_answers(dispatched_query)
           block = await self._amemory_block(step_query, "step")
           return f"{query}\n\n{block}" if block else query
   ```
   (`_drain_operator_events`, `_new_run_memory`, `_memory_records` already exist from Task 3.)
2. `AsyncSupervisor.__init__`: the same four parameters, docstring entries (say "a slow store is called off the event loop") and body lines as Task 3 step 4 (`self.memory = validate_memory(memory)`, the `memory_top_k >= 0` check, `self.memory_top_k`, `self.memory_min_score`, `self.memory_write`, `self._run_memory = None`).
3. `AsyncSupervisor._plan_once`: `user_task=await self._aplanning_task(user_task)` in the format call (it is already an `async def`), and
   ```python
           if offer:
               prompt = prompt + ask_instruction(self._operator.remaining())
               if await self._amemory_block(user_task, "plan"):
                   prompt = prompt + MEMORY_ASK_LINE
   ```
4. `AsyncSupervisor._run_subtask`: replace the `dispatched_query = self._with_answers(...)` expression with
   ```python
           dispatched_query = await self._astep_context(
               _build_augmented_query(sub_query, prior_results)
               if prior_results else sub_query,
               sub_query,
           )
   ```
5. `astream()`: mirror Task 3 step 7: create `self._run_memory` and hand it to the channel after the operator-channel lines; `stopped.memory = self._memory_records()` in the budget-stop branch and `memory=self._memory_records()` in the no-plan result; compute `outcome` once and save the run result before building the final result:
   ```python
           outcome = _supervisor_outcome(subtask_results, budget_reason)
           if self._run_memory is not None and outcome == OUTCOME_DONE:
               await asyncio.to_thread(self._run_memory.remember_result, user_task, final)
   ```
   and add `memory=self._memory_records(),` to the final `SupervisorResult(...)`, using `outcome=outcome`. Events need no new drain (the existing `for ev in self._drain_operator_events(): yield ev` loops drain memory events too).

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_supervisor_memory_wiring_async.py -q`, `python -W error -m pytest tests/test_supervisor_memory_wiring_async.py tests/test_supervisor_memory_wiring.py tests/test_supervisor_memory.py tests/test_operator_memory.py -q`, then `python -m pytest -q`
Expected: all pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_supervisor_memory_wiring_async.py
git commit -m "feat(supervisor): memory= for AsyncSupervisor, with store calls off the event loop"
```

---

### Task 5: Documentation, demo, changelog

**Files:**
- Modify: `docs/guides/sub-agents.md`, `docs/cookbook/patterns.md`, `docs/cookbook/faq.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`, `CHANGELOG.md`, `examples/subagents_demo.py`
- Regenerate: `host/data.js` via `python host/build_data.py`

**Interfaces:** Consumes the finished feature. Produces docs that match it.

- [ ] **Step 1: Guide** — in `docs/guides/sub-agents.md` add a `## Long-term memory` section after "Asking the operator" (match the file's heading style; verify every claim against `agentx_dev/SupervisorMemory.py` and the Supervisor code). Required content:
  - What it is and the one-line example:
    ```python
    from agentx_dev import HashEmbeddings, Supervisor, VectorStore

    store = VectorStore(embeddings=HashEmbeddings())          # any store with add() and search()
    supervisor = Supervisor(model=model, agents=agents, ask_user=True, memory=store)
    ```
  - Stores accepted: `VectorStore` (it has `save()` / `load()`; saving is yours to call), `ChromaVectorStore`, `QdrantVectorStore`, `PgVectorStore`.
  - The four arguments and defaults (`memory`, `memory_top_k=4`, `memory_min_score=0.2` with the 0.5 / 0.1 tuning hint, `memory_write=True`).
  - What is written (each operator answer under id `operator_answer:<16 hex of sha1 of the normalized question>`; each `done` run's final answer, cut at 2,000 characters, under `run_result:<16 hex of sha1 of the normalized task>`; nothing for unfinished runs) and what is read (the FROM MEMORY block: planner prompts and every dispatched step, never synthesis or `delegate` helpers; caps 4 items, 600 characters each, 3,000 total).
  - Exact-answer reuse (no prompt, no slot, `from_memory: True` on the record and the `answer` event) and how to refresh a fact (delete its id from the store).
  - Facts you add yourself with `store.add([...])` are shown as `[note]`.
  - `memory` stream event and `result.memory`.
  - Safety: stored results can carry text from web pages and are replayed into later prompts, so use `memory_write=False` for untrusted input; never answer with a password, key or token; costs (one embedding request per lookup); `memory_top_k=0` skips recall.

- [ ] **Step 2: Cookbook** — read pattern 32 in `docs/cookbook/patterns.md` and mirror its structure for a new `## 33. ...` "Give the Supervisor a long-term memory": the problem (asked the same thing every run), a file-backed example (parseable, uses `VectorStore.load` / `save`):
  ```python
  from pathlib import Path

  from agentx_dev import GPT, HashEmbeddings, SpawnConfig, Supervisor, VectorStore

  MEMORY_FILE = Path("supervisor_memory.json")
  embeddings = HashEmbeddings()            # offline; use OpenAIEmbeddings() for better recall
  store = VectorStore.load(MEMORY_FILE, embeddings) if MEMORY_FILE.exists() else VectorStore(embeddings)

  supervisor = Supervisor(
      model=GPT(model="gpt-4o-mini"), agents={}, ask_user=True,
      spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
      memory=store, memory_min_score=0.1,
  )
  result = supervisor.run("Compare the pricing pages of our three competitors")
  store.save(MEMORY_FILE)                  # the store is yours to persist
  print(result.memory)                     # what this run saved
  ```
  a Chroma example (read `agentx_dev/VectorStores/chroma_store.py` `ChromaVectorStore.__init__` and use its real parameters), and a read-only curated store example (`memory_write=False` with `store.add(["Our fiscal year starts in April."])`). Add a "Things to know" list (exact-match reuse, refreshing a fact, injection risk, cost). Update any index or count that lists the patterns.

- [ ] **Step 3: FAQ, reference, overview** — `docs/cookbook/faq.md`: "How do I stop the Supervisor asking me the same thing every run?" and "Can I give the Supervisor facts it should always know?". `docs/reference/api-summary.md`: add `memory`, `memory_top_k`, `memory_min_score`, `memory_write` to the Supervisor/AsyncSupervisor rows and `SupervisorResult.memory`, and the `memory` event. `docs/advanced/supervisor.md` and `README.md`: one short paragraph or table row each linking to the guide section.

- [ ] **Step 4: CHANGELOG** — in the `[3.6.0]` "Added" list add one bullet: "**Supervisor long-term memory**: `Supervisor(memory=store)` (any vector store: `VectorStore`, Chroma, Qdrant, pgvector) gives the planner and every dispatched step the few relevant stored items as a FROM MEMORY block, saves each operator answer and each completed run's final answer, and answers an exact repeat of an answered question from memory. `memory_top_k`, `memory_min_score`, `memory_write`, `SupervisorResult.memory`, `memory` stream events." Do not change the date or version header.

- [ ] **Step 5: Demo** — `examples/subagents_demo.py`: add a `--memory FILE` option. Parse it so the file name is not mistaken for the task (`--memory` takes the next argument). When given: build `HashEmbeddings()`, load the store from FILE if it exists else create `VectorStore(embeddings)`, pass `memory=store` (and `memory_min_score=0.1`) to the Supervisor (add a `memory=None` parameter to `build_supervisor`), call `store.save(FILE)` after the run, and print `result.memory` entries (`kind`, first 60 characters of `text`). Update the module docstring with `python examples/subagents_demo.py --ask --memory memory.json   # the second run does not ask again`. Keep the file importable without an API key, and do not run it against a model.

- [ ] **Step 6: Verify** — run `python host/build_data.py`, `python -m pytest -q`, `python -c "import ast; ast.parse(open('examples/subagents_demo.py').read())"`, and `ast.parse` every ```python block in the changed docs (the same check used for earlier doc tasks). `git status` must show only the files named above plus `host/data.js` (delete any stray `workspace/` folder a demo run created).

- [ ] **Step 7: Commit**

```bash
git add docs README.md CHANGELOG.md examples host
git commit -m "docs: Supervisor long-term memory (guide, cookbook 33, FAQ, demo --memory, changelog)"
```
