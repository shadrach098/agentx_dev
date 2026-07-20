"""Shape-conformance tests for the vector store adapters.

We can't actually run Chroma / Qdrant / Postgres in CI without extra
setup, so these tests focus on:
  1. Imports succeed (the adapters exist).
  2. Constructing without the underlying SDK raises a helpful ImportError.
  3. The public method surface matches the in-memory VectorStore.
"""

import pytest

from agentx_dev import HashEmbeddings, VectorStore


def method_signatures(cls):
    """Names of the public methods on a class."""
    return {
        name for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    }


class TestAdaptersImport:

    def test_module_imports(self):
        from agentx_dev.VectorStores import (
            ChromaVectorStore, QdrantVectorStore, PgVectorStore,
        )
        assert ChromaVectorStore is not None
        assert QdrantVectorStore is not None
        assert PgVectorStore is not None


class TestAdapterShapeMatchesVectorStore:
    """Every adapter must expose the same methods as the in-memory VectorStore."""

    @pytest.mark.parametrize("adapter_name", [
        "ChromaVectorStore", "QdrantVectorStore", "PgVectorStore",
    ])
    def test_public_shape_matches(self, adapter_name):
        from agentx_dev import VectorStores as adapters
        adapter = getattr(adapters, adapter_name)
        base = method_signatures(VectorStore)
        got = method_signatures(adapter)
        # Every method the in-memory store exposes should exist on the adapter.
        missing = base - got
        # save/load are in-memory-specific; adapters use their DB's persistence.
        allowed_missing = {"save", "load"}
        missing = missing - allowed_missing
        assert not missing, f"{adapter_name} missing {missing}"

    @pytest.mark.parametrize("adapter_name", [
        "ChromaVectorStore", "QdrantVectorStore", "PgVectorStore",
    ])
    def test_has_embeddings_property(self, adapter_name):
        from agentx_dev import VectorStores as adapters
        adapter = getattr(adapters, adapter_name)
        # embeddings is a property; check the class attribute exists.
        assert hasattr(adapter, "embeddings")


class TestFriendlyImportError:
    """When the underlying SDK isn't installed, construction should raise a
    clear ImportError -- not a generic ModuleNotFoundError from deep inside."""

    def test_chroma_without_chromadb(self, monkeypatch):
        import sys
        # If chromadb IS installed on the test machine, skip.
        try:
            import chromadb  # noqa: F401
            pytest.skip("chromadb is installed on this machine")
        except ImportError:
            pass
        from agentx_dev.VectorStores import ChromaVectorStore
        with pytest.raises(ImportError) as exc:
            ChromaVectorStore(embeddings=HashEmbeddings())
        assert "chromadb" in str(exc.value)

    def test_qdrant_without_client(self, monkeypatch):
        try:
            import qdrant_client  # noqa: F401
            pytest.skip("qdrant-client is installed on this machine")
        except ImportError:
            pass
        from agentx_dev.VectorStores import QdrantVectorStore
        with pytest.raises(ImportError) as exc:
            QdrantVectorStore(embeddings=HashEmbeddings())
        assert "qdrant" in str(exc.value).lower()

    def test_pgvector_without_psycopg(self, monkeypatch):
        try:
            import psycopg  # noqa: F401
            pytest.skip("psycopg is installed on this machine")
        except ImportError:
            pass
        try:
            import psycopg2  # noqa: F401
            pytest.skip("psycopg2 is installed on this machine")
        except ImportError:
            pass
        from agentx_dev.VectorStores import PgVectorStore
        with pytest.raises(ImportError) as exc:
            PgVectorStore(embeddings=HashEmbeddings(), dsn="postgresql://x")
        assert "psycopg" in str(exc.value).lower()
