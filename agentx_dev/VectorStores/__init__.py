"""Vector store adapters for production RAG.

The in-memory ``VectorStore`` in ``agentx_dev.Embeddings`` fits a few
thousand chunks and one process. When you outgrow that, drop in one of
these adapters -- they present the SAME public shape (``add``,
``search``, ``delete``, ``clear``, ``__len__``) so ``vector_search_tool``
and ``SemanticMemory`` accept them without any other code changes.

Adapters ship:

- ``ChromaVectorStore``  -- Chroma (local persistent OR client/server).
- ``QdrantVectorStore``  -- Qdrant (local OR remote).
- ``PgVectorStore``      -- Postgres + pgvector extension.

Each adapter imports its underlying SDK lazily, so
``import agentx_dev.VectorStores`` does not require any of them.
Install only what you use::

    pip install chromadb              # ChromaVectorStore
    pip install qdrant-client         # QdrantVectorStore
    pip install psycopg[binary]       # PgVectorStore  (or psycopg2-binary)

Every adapter shares one contract:

    class MyVectorStore:
        def add(self, texts, *, ids=None, metadata=None) -> list[str]: ...
        def search(self, query, *, top_k=5, min_score=0.0) -> list[VectorHit]: ...
        def delete(self, ids) -> int: ...
        def clear(self) -> None: ...
        def __len__(self) -> int: ...
        embeddings: Embeddings   # so SemanticMemory can share the backend
"""

from .chroma_store import ChromaVectorStore
from .qdrant_store import QdrantVectorStore
from .pg_store import PgVectorStore


__all__ = [
    "ChromaVectorStore",
    "QdrantVectorStore",
    "PgVectorStore",
]
