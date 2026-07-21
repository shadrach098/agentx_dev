"""
Embeddings + vector storage + semantic memory + RAG tool for AgentX.

This module adds four things that let agents remember and retrieve at scale
without a heavyweight vector-DB dependency:

1. ``Embeddings`` -- abstract base with two ready-made backends:
     * ``OpenAIEmbeddings``  -- calls OpenAI's ``text-embedding-3-small`` (default).
       Requires ``OPENAI_API_KEY``.
     * ``HashEmbeddings``    -- zero-dependency fallback that hashes token
       ngrams into a fixed-dim vector. Not semantically great, but
       deterministic, needs no network, and lets tests exercise the
       retrieval path without a real embedding provider.

2. ``VectorStore`` -- small in-process store. numpy-accelerated when
   available, pure-python cosine otherwise. Save/load to a JSON file
   so RAG collections persist across runs.

3. ``SemanticMemory`` -- a ``BaseMemory`` implementation that embeds every
   message and retrieves the top-K most relevant past turns for the
   current query, injected as a synthetic system message. Turns older
   than the retrieval window are compressed out of the working history
   but still recoverable via similarity.

4. ``vector_search_tool()`` -- factory returning a ``StructuredTool`` that
   lets the agent query any ``VectorStore`` at runtime (RAG). Same shape
   as ``web_search_tool()``: pass into the runner's ``tools=`` list.

Design notes:
- No hard numpy dependency. If numpy is imported at runtime, we use it;
  otherwise the pure-python cosine path handles it.
- No hard OpenAI dep either -- the OpenAI backend imports lazily so
  ``import agentx_dev`` doesn't need it.
- Vector dim is set by the backend, not the store -- the store validates
  at ``add`` time and raises on mismatch, which is a common footgun
  when someone changes embedding model mid-collection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agentx_dev.Memory import BaseMemory, Message

try:  # optional; the pure-python fallback works without it
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _np = None
    _HAS_NUMPY = False


__all__ = [
    "Embeddings",
    "OpenAIEmbeddings",
    "HashEmbeddings",
    "VectorStore",
    "VectorHit",
    "SemanticMemory",
    "vector_search_tool",
]


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------


class Embeddings(ABC):
    """Abstract base. Implementations produce a fixed-dim vector per text."""

    dim: int = 0

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one vector per input text. Length of the outer list matches
        the input; inner list length equals ``self.dim``."""

    def embed_one(self, text: str) -> List[float]:
        """Convenience: embed a single string."""
        return self.embed([text])[0]


class OpenAIEmbeddings(Embeddings):
    """OpenAI-backed embeddings.

    Defaults to ``text-embedding-3-small`` (1536 dims, fast, cheap). Set
    ``model=`` to switch -- e.g. ``text-embedding-3-large`` (3072 dims,
    higher quality).

    Reads ``OPENAI_API_KEY`` from environment; pass ``api_key=`` to override.
    """

    _MODEL_DIMS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        batch_size: int = 100,
    ):
        from openai import OpenAI as _OpenAI

        self._client = _OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url,
        )
        self.model = model
        self.batch_size = batch_size
        self.dim = self._MODEL_DIMS.get(model, 1536)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        # OpenAI recommends batching -- 100 is a safe default well under the
        # documented per-request cap.
        for start in range(0, len(texts), self.batch_size):
            chunk = list(texts[start: start + self.batch_size])
            response = self._client.embeddings.create(input=chunk, model=self.model)
            out.extend(item.embedding for item in response.data)
        # First call -- snap the true dim (some fine-tuned models differ).
        if out and self.dim != len(out[0]):
            self.dim = len(out[0])
        return out


class HashEmbeddings(Embeddings):
    """Zero-dependency embedding fallback.

    Hashes character-level trigrams into a fixed-dim vector, then L2-
    normalizes. Reproducible (same text -> same vector across runs and
    machines), fast, and no network. NOT semantically strong -- near-
    misses on paraphrase, decent on keyword overlap. Use for tests and
    development; swap to ``OpenAIEmbeddings`` for real RAG quality.
    """

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("HashEmbeddings dim must be positive")
        self.dim = int(dim)

    def _vectorize(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        if not text:
            return vec
        s = text.lower()
        # Character-3-grams handle unicode + short strings gracefully.
        grams = [s[i:i + 3] for i in range(max(1, len(s) - 2))] or [s]
        for gram in grams:
            h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "little")
            idx = h % self.dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vectorize(t) for t in texts]


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------


@dataclass
class VectorHit:
    """One result from ``VectorStore.search``. ``score`` is cosine similarity
    in [-1, 1]; higher = more similar."""
    id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class VectorStore:
    """Small in-process vector store with cosine similarity.

    Not a replacement for Chroma / Qdrant / pgvector at scale, but perfect
    for the "a few thousand chunks + one agent" case that covers most RAG
    workflows in prototypes and small production apps. Backed by numpy
    when available (fast dot-product) and pure Python otherwise.

    Persistence: ``save(path)`` writes a JSON file. ``load(path, embeddings=)``
    rehydrates the store. The embeddings backend is NOT persisted --
    callers reattach the same one they used to populate the store.
    """

    def __init__(self, embeddings: Embeddings):
        self._embeddings = embeddings
        self._ids: List[str] = []
        self._texts: List[str] = []
        self._vectors: List[List[float]] = []
        self._metadata: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    @property
    def embeddings(self) -> Embeddings:
        return self._embeddings

    def __len__(self) -> int:
        return len(self._ids)

    def add(
        self,
        texts: Sequence[str],
        *,
        ids: Optional[Sequence[str]] = None,
        metadata: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[str]:
        """Embed ``texts`` and add them to the store. Returns the ids used.

        ``ids``: auto-generated (``doc_{n}``) when omitted; unique across the
        store -- passing an existing id overwrites the old vector.
        ``metadata``: optional per-doc dict (e.g. ``{"source": "file.md"}``).
        """
        if not texts:
            return []
        texts = list(texts)
        if ids is None:
            next_n = len(self._ids)
            ids = [f"doc_{next_n + i}" for i in range(len(texts))]
        else:
            ids = list(ids)
            if len(ids) != len(texts):
                raise ValueError("ids length must match texts length")
        if metadata is None:
            metadata = [{} for _ in texts]
        else:
            metadata = list(metadata)
            if len(metadata) != len(texts):
                raise ValueError("metadata length must match texts length")

        vectors = self._embeddings.embed(texts)
        if not vectors:
            return list(ids)
        dim = len(vectors[0])
        # Enforce dim consistency -- a common footgun is swapping the embedding
        # model mid-collection and getting garbage cosine scores.
        if self._vectors and len(self._vectors[0]) != dim:
            raise ValueError(
                f"Embedding dim mismatch: store has {len(self._vectors[0])}, "
                f"new vectors have {dim}. Reindex the collection with the new "
                f"embedding model or use a separate store."
            )

        with self._lock:
            existing_by_id = {i: pos for pos, i in enumerate(self._ids)}
            for i, text, vec, meta in zip(ids, texts, vectors, metadata):
                if i in existing_by_id:
                    pos = existing_by_id[i]
                    self._texts[pos] = text
                    self._vectors[pos] = list(vec)
                    self._metadata[pos] = dict(meta)
                else:
                    self._ids.append(i)
                    self._texts.append(text)
                    self._vectors.append(list(vec))
                    self._metadata.append(dict(meta))
        return list(ids)

    def add_documents(
        self,
        docs: Sequence[Any],
        *,
        ids: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Ergonomic add for ``Document`` objects (see
        ``agentx_dev.Splitters.Document``). Unpacks ``doc.text`` and
        ``doc.metadata`` for you. Complements ``add()`` when your
        upstream is a ``TextSplitter``.

        Any object with ``.text`` and ``.metadata`` attributes works --
        no hard dependency on the Document dataclass, so callers can
        pass their own doc-shaped types.
        """
        if not docs:
            return []
        texts = [d.text for d in docs]
        metadata = [dict(getattr(d, "metadata", {}) or {}) for d in docs]
        return self.add(texts, ids=ids, metadata=metadata)

    def delete(self, ids: Sequence[str]) -> int:
        """Remove docs by id. Returns count removed."""
        removed = 0
        drop = set(ids)
        with self._lock:
            keep_idx = [i for i, docid in enumerate(self._ids) if docid not in drop]
            removed = len(self._ids) - len(keep_idx)
            self._ids = [self._ids[i] for i in keep_idx]
            self._texts = [self._texts[i] for i in keep_idx]
            self._vectors = [self._vectors[i] for i in keep_idx]
            self._metadata = [self._metadata[i] for i in keep_idx]
        return removed

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()
            self._texts.clear()
            self._vectors.clear()
            self._metadata.clear()

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> List[VectorHit]:
        """Return the top-K most similar docs to ``query``.

        ``min_score`` filters out weak matches. 0.0 = keep everything.
        For OpenAI embeddings, 0.5 is a reasonable "relevant" threshold;
        for HashEmbeddings, use 0.1 (it's noisier).
        """
        if not self._vectors or top_k <= 0:
            return []
        qvec = self._embeddings.embed_one(query)
        scores = self._score_all(qvec)
        # Take top_k
        ranked = sorted(enumerate(scores), key=lambda kv: kv[1], reverse=True)[:top_k]
        hits: List[VectorHit] = []
        for idx, score in ranked:
            if score < min_score:
                continue
            hits.append(VectorHit(
                id=self._ids[idx],
                text=self._texts[idx],
                score=float(score),
                metadata=dict(self._metadata[idx]),
            ))
        return hits

    def _score_all(self, qvec: Sequence[float]) -> List[float]:
        """Cosine similarity of ``qvec`` against every stored vector.

        The stored vectors from the embedding backends we ship are already
        (approximately) unit-length: OpenAI's are unit-norm by contract,
        and HashEmbeddings L2-normalizes explicitly. We still divide by
        the actual norms for defensive correctness -- the cost is
        negligible against the dot-product itself.
        """
        if _HAS_NUMPY:
            mat = _np.asarray(self._vectors, dtype=_np.float32)
            q = _np.asarray(qvec, dtype=_np.float32)
            mat_norm = _np.linalg.norm(mat, axis=1)
            q_norm = float(_np.linalg.norm(q))
            denom = _np.where(mat_norm == 0, 1.0, mat_norm) * (q_norm or 1.0)
            sims = (mat @ q) / denom
            return sims.tolist()
        # Pure python
        q_norm = math.sqrt(sum(x * x for x in qvec)) or 1.0
        out: List[float] = []
        for vec in self._vectors:
            dot = sum(a * b for a, b in zip(vec, qvec))
            v_norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append(dot / (v_norm * q_norm))
        return out

    def save(self, path: Any) -> Path:
        """Write the store to a JSON file. Embeddings backend is NOT saved."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "dim": len(self._vectors[0]) if self._vectors else 0,
            "docs": [
                {"id": docid, "text": text, "vector": vec, "metadata": meta}
                for docid, text, vec, meta in zip(
                    self._ids, self._texts, self._vectors, self._metadata,
                )
            ],
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Any, embeddings: Embeddings) -> "VectorStore":
        """Rehydrate a store from disk. ``embeddings`` should be the SAME
        backend + model used to populate it, or search scores will be
        meaningless (dim-mismatch will raise; semantic drift won't)."""
        p = Path(path)
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        store = cls(embeddings=embeddings)
        docs = payload.get("docs", [])
        for doc in docs:
            store._ids.append(str(doc["id"]))
            store._texts.append(str(doc["text"]))
            store._vectors.append([float(x) for x in doc["vector"]])
            store._metadata.append(dict(doc.get("metadata") or {}))
        return store


# ---------------------------------------------------------------------------
# Semantic memory (long-term retrieval-augmented conversation history)
# ---------------------------------------------------------------------------


class SemanticMemory(BaseMemory):
    """Conversation memory with semantic retrieval.

    Every added message is stored in a ``VectorStore`` alongside its role.
    When ``get_messages()`` is called with a query, the recent-tail
    messages are kept verbatim and the top-K most relevant older messages
    are pulled from the store and prepended as a synthetic system note.

    This gives an agent a "long tail" of memory: it can recall relevant
    facts from thousands of turns ago without paying to pass them all in
    every prompt. Compare to ``SlidingWindowMemory`` (drops older turns)
    and ``SummaryMemory`` (compresses them via an LLM).

    Usage:

        mem = SemanticMemory(embeddings=OpenAIEmbeddings())
        mem.add_message("user", "My dog Rex was born in 2019.")
        mem.add_message("assistant", "Noted -- Rex, born 2019.")
        # ...many turns later...
        mem.set_query("When was my dog born?")
        history = mem.get_messages()   # top-K includes the Rex message
    """

    def __init__(
        self,
        embeddings: Embeddings,
        *,
        recent_tail: int = 6,
        top_k: int = 4,
        min_score: float = 0.15,
        preserve_system: bool = True,
    ):
        """
        Args:
            embeddings: An ``Embeddings`` instance. Reuse the same one
                across sessions or you'll get dim-mismatch on ``add``.
            recent_tail: Number of most-recent messages kept verbatim in
                the working history regardless of relevance.
            top_k: How many older messages to retrieve semantically.
            min_score: Cosine-similarity floor for retrieved messages
                (default 0.15 -- permissive; tune up for OpenAI, down for
                the hash fallback).
            preserve_system: Always keep the system message at the top.
        """
        self._store = VectorStore(embeddings=embeddings)
        self._all_messages: List[Message] = []
        self._system_message: Optional[Message] = None
        self._query: str = ""
        self.recent_tail = int(recent_tail)
        self.top_k = int(top_k)
        self.min_score = float(min_score)
        self.preserve_system = bool(preserve_system)

    @property
    def store(self) -> VectorStore:
        """Underlying vector store -- expose so callers can inspect / persist."""
        return self._store

    def set_query(self, query: str) -> None:
        """Set the query used by ``get_messages`` to score older turns.

        The runner should call this before each iteration so retrieval
        stays focused on what the user just asked. Left empty, only the
        recent-tail is returned (no semantic recall).
        """
        self._query = str(query or "")

    def add_message(self, role: str, content: str, **kwargs) -> None:
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get("metadata", {}),
            importance=kwargs.get("importance", 1.0),
        )
        if role == "system" and self.preserve_system:
            self._system_message = message
            return
        self._all_messages.append(message)
        # Store retrievable content; only user/assistant/tool add signal to search.
        doc_id = f"msg_{len(self._all_messages) - 1}"
        self._store.add(
            [content],
            ids=[doc_id],
            metadata=[{
                "role": role,
                "timestamp": message.timestamp.isoformat(),
                "importance": message.importance,
            }],
        )

    def get_messages(self) -> List[Dict[str, str]]:
        """Return system + retrieved-context + recent-tail messages."""
        out: List[Dict[str, str]] = []
        if self._system_message and self.preserve_system:
            out.append(self._system_message.to_dict())

        recent = self._all_messages[-self.recent_tail:] if self.recent_tail > 0 else []
        recent_indices = {
            id(m) for m in recent
        }

        # Retrieve older relevant messages by similarity.
        if self._query and len(self._all_messages) > self.recent_tail:
            hits = self._store.search(
                self._query, top_k=self.top_k, min_score=self.min_score,
            )
            # Rehydrate hit doc_ids -> Message objects, skipping any that
            # are already in the recent-tail slice (no duplication).
            snippets: List[str] = []
            for hit in hits:
                try:
                    idx = int(hit.id.split("_", 1)[1])
                except (IndexError, ValueError):
                    continue
                if idx < 0 or idx >= len(self._all_messages):
                    continue
                msg = self._all_messages[idx]
                if id(msg) in recent_indices:
                    continue
                snippets.append(f"[{msg.role} @ {msg.timestamp.isoformat()}] {msg.content}")
            if snippets:
                out.append({
                    "role": "system",
                    "content": (
                        "RELEVANT PRIOR CONTEXT (retrieved from long-term memory "
                        "based on similarity to the current query):\n\n"
                        + "\n---\n".join(snippets)
                    ),
                })

        out.extend(m.to_dict() for m in recent)
        return out

    def clear(self) -> None:
        self._all_messages.clear()
        self._system_message = None
        self._store.clear()
        self._query = ""


# ---------------------------------------------------------------------------
# vector_search RAG tool
# ---------------------------------------------------------------------------


def vector_search_tool(
    store: VectorStore,
    *,
    name: str = "vector_search",
    default_top_k: int = 5,
    max_top_k: int = 20,
    description: Optional[str] = None,
):
    """Build a StructuredTool that queries ``store`` for the top-K matches.

    Usage:

        from agentx_dev import AgentRunner, AgentType, Claude, Permissions
        from agentx_dev.Embeddings import (
            HashEmbeddings, VectorStore, vector_search_tool,
        )

        store = VectorStore(embeddings=HashEmbeddings())
        store.add(
            ["The mitochondrion is the powerhouse of the cell.",
             "Python's GIL prevents true multi-thread parallelism."],
        )

        runner = AgentRunner(
            model=Claude(),
            agent=AgentType.ReAct,
            tools=[vector_search_tool(store)],
        )
        runner.invoke("What's Python's GIL?")

    Args:
        store: The ``VectorStore`` to query.
        name: Tool name the LLM will see (default ``vector_search``).
        default_top_k: Fallback ``top_k`` when the LLM doesn't specify one.
        max_top_k: Hard cap the LLM can't exceed (protects the prompt from
            "give me 500 docs" runaway queries).
        description: Override the default description if you want to hint
            the model at what's in the store (e.g. "Search internal docs
            for policy questions").
    """
    from pydantic import BaseModel, Field
    from agentx_dev.Tools import StructuredTool

    class VectorSearchArgs(BaseModel):
        query: str = Field(..., description="Natural-language query.")
        top_k: int = Field(
            default_top_k,
            description=f"How many results to return (1-{max_top_k}).",
        )
        min_score: float = Field(
            0.0,
            description=(
                "Filter out hits with cosine similarity below this "
                "threshold (0.0 = keep everything)."
            ),
        )

    def _search(query: str, top_k: int = default_top_k, min_score: float = 0.0) -> str:
        capped = max(1, min(int(top_k), max_top_k))
        hits = store.search(query, top_k=capped, min_score=min_score)
        if not hits:
            return f"No results matching {query!r}."
        lines = [f"Top {len(hits)} results for {query!r}:"]
        for i, hit in enumerate(hits, 1):
            preview = hit.text.strip().replace("\n", " ")
            if len(preview) > 500:
                preview = preview[:500] + "..."
            meta = ""
            if hit.metadata:
                meta = f" | metadata: {hit.metadata}"
            lines.append(
                f"{i}. [id={hit.id} score={hit.score:.3f}{meta}]\n   {preview}"
            )
        return "\n".join(lines)

    return StructuredTool(
        func=_search,
        args_schema=VectorSearchArgs,
        name=name,
        description=description or (
            "Semantic search over a private document collection. Returns "
            "the top-K passages most similar to the query, ranked by "
            "cosine similarity. Use for RAG: retrieve relevant passages "
            "before answering knowledge questions."
        ),
    )
