"""Chroma-backed VectorStore adapter.

Usage:

    from agentx_dev import OpenAIEmbeddings, vector_search_tool
    from agentx_dev.VectorStores import ChromaVectorStore

    store = ChromaVectorStore(
        embeddings=OpenAIEmbeddings(),
        collection_name="my_docs",
        persist_directory="./.chroma",  # or None for in-memory
    )
    store.add(["fact one", "fact two"])
    tool = vector_search_tool(store)

Chroma handles persistence + concurrency itself. This adapter just
maps our ``add`` / ``search`` shape onto Chroma's client API and lets
the shared Embeddings backend compute vectors so cosine scores stay
comparable across stores.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from agentx_dev.Embeddings import Embeddings, VectorHit


class ChromaVectorStore:
    """Adapter for Chroma. Same public shape as ``VectorStore``."""

    def __init__(
        self,
        embeddings: Embeddings,
        *,
        collection_name: str = "agentx",
        persist_directory: Optional[str] = None,
        client: Any = None,
        distance: str = "cosine",
    ):
        """
        Args:
            embeddings: Any ``Embeddings`` instance. The framework computes
                vectors client-side so scores stay comparable with the
                in-memory store and SemanticMemory can share the backend.
            collection_name: Chroma collection to read/write.
            persist_directory: Where Chroma stores data on disk. None ->
                in-memory only (goes away on process exit).
            client: Optionally pass a pre-configured Chroma client. When
                given, ``persist_directory`` is ignored.
            distance: One of ``cosine``, ``l2``, ``ip``. Cosine matches
                the default ``VectorStore`` semantics.
        """
        try:
            import chromadb
        except ImportError as e:
            raise ImportError(
                "ChromaVectorStore requires the chromadb package. "
                "Install with: pip install chromadb"
            ) from e

        self._embeddings = embeddings
        if client is None:
            if persist_directory:
                client = chromadb.PersistentClient(path=persist_directory)
            else:
                client = chromadb.Client()
        self._client = client
        # get_or_create so calls on existing collections are idempotent.
        self._collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": distance},
        )

    @property
    def embeddings(self) -> Embeddings:
        return self._embeddings

    def __len__(self) -> int:
        return int(self._collection.count())

    def add(
        self,
        texts: Sequence[str],
        *,
        ids: Optional[Sequence[str]] = None,
        metadata: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[str]:
        if not texts:
            return []
        texts = list(texts)
        if ids is None:
            base = self._collection.count()
            ids = [f"doc_{base + i}" for i in range(len(texts))]
        else:
            ids = [str(x) for x in ids]
        vectors = self._embeddings.embed(texts)
        # Chroma requires non-empty metadata dicts -- fall back to {"_": 0}.
        metas = (
            [dict(m or {"_": 0}) for m in metadata]
            if metadata is not None
            else [{"_": 0}] * len(texts)
        )
        # Upsert so existing ids get replaced, matching VectorStore semantics.
        self._collection.upsert(
            ids=list(ids),
            documents=list(texts),
            embeddings=vectors,
            metadatas=metas,
        )
        return list(ids)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> List[VectorHit]:
        if top_k <= 0 or len(self) == 0:
            return []
        qvec = self._embeddings.embed_one(query)
        result = self._collection.query(
            query_embeddings=[qvec],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        # Chroma returns distance for cosine (1 - similarity). Convert.
        distances = result.get("distances", [[]])[0]
        hits: List[VectorHit] = []
        for i, doc_id in enumerate(ids):
            distance = float(distances[i]) if i < len(distances) else 0.0
            score = 1.0 - distance
            if score < min_score:
                continue
            hits.append(VectorHit(
                id=str(doc_id),
                text=str(docs[i]) if i < len(docs) else "",
                score=score,
                metadata=dict(metas[i] or {}) if i < len(metas) else {},
            ))
        return hits

    def add_documents(self, docs, *, ids=None):
        """Same shape as ``VectorStore.add_documents`` -- unpacks
        ``doc.text`` and ``doc.metadata`` and delegates to ``add()``."""
        if not docs:
            return []
        texts = [d.text for d in docs]
        metadata = [dict(getattr(d, "metadata", {}) or {}) for d in docs]
        return self.add(texts, ids=ids, metadata=metadata)

    def delete(self, ids: Sequence[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        pre = len(self)
        self._collection.delete(ids=ids)
        return pre - len(self)

    def clear(self) -> None:
        # Deleting the whole collection and recreating is the cleanest way.
        name = self._collection.name
        metadata = self._collection.metadata
        self._client.delete_collection(name=name)
        self._collection = self._client.get_or_create_collection(
            name=name, metadata=metadata,
        )
