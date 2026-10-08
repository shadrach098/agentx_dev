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

from agentx_dev.Operator import MAX_ANSWER_CHARS, _normalize
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
        self._epoch = 0                               # bumped by note_answered; guards the cache
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

    def _format(self, hits: List[Any], known: Set[str]) -> Tuple[str, int]:
        lines: List[str] = []
        used = len(MEMORY_HEADER)
        for h in hits:
            try:
                meta = getattr(h, "metadata", None) or {}
                if meta.get("kind") == KIND_ANSWER and meta.get("qkey") in known:
                    continue                          # already in this run's OPERATOR ANSWERS
                text = " ".join(str(getattr(h, "text", "")).split())
                if len(text) > MEMORY_ITEM_CHARS:
                    text = text[: MEMORY_ITEM_CHARS - 3] + "..."
                line = f"- [{_label(meta)}] {text}"
            except Exception as e:                    # a malformed hit is skipped, never raised
                logger.warning(f"memory hit skipped (malformed): {e}")
                continue
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
                epoch, known = self._epoch, set(self._known)
            found = self._search(query, self.top_k, self.min_score)
            block, hits = self._format(found, known)
            with self._lock:
                if epoch == self._epoch:
                    self._cache[key] = block
                else:                                 # an answer arrived mid-search: re-filter, don't cache
                    known = set(self._known)
                    block, hits = self._format(found, known)
        if block:
            self._announce(stage, hits)
        return block

    async def arecall(self, query: str, stage: str) -> str:
        return await asyncio.to_thread(self.recall, query, stage)

    def lookup_answer(self, question: str) -> Optional[str]:
        """The stored operator answer to exactly this question (after normalization), or None."""
        qkey = _normalize(question)
        for h in self._search(question, 5, 0.0):
            try:
                meta = getattr(h, "metadata", None) or {}
                if meta.get("kind") == KIND_ANSWER and meta.get("qkey") == qkey:
                    answer = meta.get("answer")
                    if isinstance(answer, str) and answer.strip():
                        return answer
            except Exception as e:                    # a malformed hit is skipped, never raised
                logger.warning(f"memory hit skipped (malformed): {e}")
        return None

    async def alookup_answer(self, question: str) -> Optional[str]:
        return await asyncio.to_thread(self.lookup_answer, question)

    def note_answered(self, question: str) -> None:
        """The operator answered ``question`` in this run: leave it out of later blocks."""
        with self._lock:
            self._known.add(_normalize(question))
            self._cache.clear()
            self._epoch += 1

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
        question, answer = str(question), str(answer)[:MAX_ANSWER_CHARS]
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
