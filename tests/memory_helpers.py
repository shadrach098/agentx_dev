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
