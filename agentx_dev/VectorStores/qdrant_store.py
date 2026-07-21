"""Qdrant-backed VectorStore adapter.

Usage (local):

    from agentx_dev import OpenAIEmbeddings
    from agentx_dev.VectorStores import QdrantVectorStore

    store = QdrantVectorStore(
        embeddings=OpenAIEmbeddings(),
        collection_name="my_docs",
        location=":memory:",       # or "http://localhost:6333"
    )

Usage (remote):

    store = QdrantVectorStore(
        embeddings=OpenAIEmbeddings(),
        collection_name="my_docs",
        url="https://<region>.qdrant.io:6333",
        api_key=os.environ["QDRANT_API_KEY"],
    )
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence

from agentx_dev.Embeddings import Embeddings, VectorHit


class QdrantVectorStore:
    """Adapter for Qdrant. Same public shape as ``VectorStore``."""

    def __init__(
        self,
        embeddings: Embeddings,
        *,
        collection_name: str = "agentx",
        location: Optional[str] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        client: Any = None,
        distance: str = "Cosine",
    ):
        """
        Args:
            embeddings: Any ``Embeddings`` instance.
            collection_name: Qdrant collection to read/write.
            location: Local qdrant location. ``":memory:"`` for in-process,
                or a path like ``"./.qdrant"`` for a local file-backed instance.
                Ignored when ``url`` or ``client`` is provided.
            url: Remote Qdrant HTTP URL.
            api_key: API key for the remote endpoint.
            client: Pre-configured ``qdrant_client.QdrantClient``.
            distance: One of ``Cosine``, ``Dot``, ``Euclid``, ``Manhattan``.
                Cosine matches the default ``VectorStore`` semantics.
        """
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as qm
        except ImportError as e:
            raise ImportError(
                "QdrantVectorStore requires qdrant-client. "
                "Install with: pip install qdrant-client"
            ) from e

        self._embeddings = embeddings
        self._collection = collection_name
        self._qm = qm
        if client is None:
            if url:
                client = QdrantClient(url=url, api_key=api_key)
            else:
                client = QdrantClient(location=location or ":memory:")
        self._client = client
        self._distance = getattr(qm.Distance, distance.upper(), qm.Distance.COSINE)

        # Ensure the collection exists with the right vector dim.
        # We probe the embedding dim by embedding one dummy string once
        # -- cached embedding backends (OpenAIEmbeddings) do a real call
        # but only once at startup.
        dim = getattr(embeddings, "dim", None)
        if not dim:
            dim = len(embeddings.embed_one("."))
            embeddings.dim = dim
        existing = {c.name for c in client.get_collections().collections}
        if collection_name not in existing:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qm.VectorParams(size=dim, distance=self._distance),
            )

    @property
    def embeddings(self) -> Embeddings:
        return self._embeddings

    def __len__(self) -> int:
        info = self._client.get_collection(self._collection)
        return int(info.points_count or 0)

    @staticmethod
    def _hash_id(text_id: str) -> str:
        """Qdrant point IDs must be UUIDs or ints. Hash string ids via
        UUID5 in a stable namespace so ``ids=['doc_A']`` stays consistent
        across calls."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, text_id))

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
            base = len(self)
            ids = [f"doc_{base + i}" for i in range(len(texts))]
        else:
            ids = [str(x) for x in ids]
        vectors = self._embeddings.embed(texts)
        metas = (
            list(metadata) if metadata is not None
            else [{} for _ in texts]
        )
        points = [
            self._qm.PointStruct(
                id=self._hash_id(ids[i]),
                vector=vectors[i],
                payload={
                    "text": texts[i],
                    "orig_id": ids[i],
                    "metadata": dict(metas[i] or {}),
                },
            )
            for i in range(len(texts))
        ]
        self._client.upsert(collection_name=self._collection, points=points)
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
        result = self._client.query_points(
            collection_name=self._collection,
            query=qvec,
            limit=top_k,
            score_threshold=min_score if min_score > 0 else None,
        )
        hits: List[VectorHit] = []
        for point in result.points:
            payload = point.payload or {}
            hits.append(VectorHit(
                id=str(payload.get("orig_id", point.id)),
                text=str(payload.get("text", "")),
                score=float(point.score),
                metadata=dict(payload.get("metadata") or {}),
            ))
        return hits

    def add_documents(self, docs, *, ids=None):
        """Same shape as ``VectorStore.add_documents``."""
        if not docs:
            return []
        texts = [d.text for d in docs]
        metadata = [dict(getattr(d, "metadata", {}) or {}) for d in docs]
        return self.add(texts, ids=ids, metadata=metadata)

    def delete(self, ids: Sequence[str]) -> int:
        ids = [self._hash_id(str(x)) for x in ids]
        if not ids:
            return 0
        pre = len(self)
        self._client.delete(
            collection_name=self._collection,
            points_selector=self._qm.PointIdsList(points=ids),
        )
        return pre - len(self)

    def clear(self) -> None:
        # Recreating the collection is the fastest way; delete_all is slow.
        info = self._client.get_collection(self._collection)
        vectors_config = info.config.params.vectors
        self._client.delete_collection(collection_name=self._collection)
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=vectors_config,
        )
