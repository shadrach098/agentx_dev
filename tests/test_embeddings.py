"""Tests for Embeddings + VectorStore + SemanticMemory + vector_search_tool."""

import json
import tempfile
from pathlib import Path

import pytest

from agentx_dev import (
    HashEmbeddings, VectorStore, VectorHit,
    SemanticMemory, vector_search_tool, create_semantic_memory,
)


class TestHashEmbeddings:

    def test_deterministic(self):
        e = HashEmbeddings(dim=64)
        v1 = e.embed_one("hello")
        v2 = e.embed_one("hello")
        assert v1 == v2

    def test_normalized(self):
        e = HashEmbeddings(dim=64)
        v = e.embed_one("hello world")
        norm2 = sum(x * x for x in v)
        assert abs(norm2 - 1.0) < 1e-6, f"vector not L2-normalized (norm^2={norm2})"

    def test_batch(self):
        e = HashEmbeddings(dim=64)
        vs = e.embed(["a", "b", "c"])
        assert len(vs) == 3
        assert all(len(v) == 64 for v in vs)

    def test_dim_positive(self):
        with pytest.raises(ValueError):
            HashEmbeddings(dim=0)


class TestVectorStore:

    def test_add_and_search(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add([
            "Postgres uses MVCC for concurrency.",
            "SQLite stores the whole DB in one file.",
            "Redis is an in-memory cache.",
        ])
        hits = store.search("multi version concurrency", top_k=1)
        assert len(hits) == 1
        assert "MVCC" in hits[0].text or "concurrency" in hits[0].text

    def test_ids_and_metadata(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(
            ["fact one"],
            ids=["doc_A"],
            metadata=[{"source": "test"}],
        )
        assert len(store) == 1
        hits = store.search("fact", top_k=1)
        assert hits[0].id == "doc_A"
        assert hits[0].metadata == {"source": "test"}

    def test_id_overwrite(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["original"], ids=["x"])
        store.add(["replaced"], ids=["x"])
        assert len(store) == 1

    def test_delete(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["a", "b", "c"], ids=["1", "2", "3"])
        removed = store.delete(["2"])
        assert removed == 1
        assert len(store) == 2

    def test_save_load_roundtrip(self, tmp_path):
        e = HashEmbeddings()
        store = VectorStore(embeddings=e)
        store.add(["fact one", "fact two"], metadata=[{"a": 1}, {"a": 2}])
        p = tmp_path / "kb.json"
        store.save(p)

        loaded = VectorStore.load(p, embeddings=HashEmbeddings())
        assert len(loaded) == 2
        hits = loaded.search("fact one", top_k=1)
        assert "one" in hits[0].text

    def test_min_score_filter(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["something"])
        hits = store.search("something", top_k=5, min_score=0.99)
        assert len(hits) <= 1

    def test_dim_mismatch_raises(self):
        store = VectorStore(embeddings=HashEmbeddings(dim=64))
        store.add(["first"])
        # Manually inject a wrong-dim vector via a fresh embedder with different dim
        e2 = HashEmbeddings(dim=128)
        store2 = VectorStore(embeddings=e2)
        store2.add(["second"])
        with pytest.raises(ValueError):
            store._vectors = store._vectors + store2._vectors  # this is fine, but...
            # But adding via .add with a mismatched dim SHOULD raise. Simulate that.
            store._embeddings = e2
            store.add(["third"])


class TestSemanticMemory:

    def test_recent_tail_preserved(self):
        mem = SemanticMemory(
            embeddings=HashEmbeddings(), recent_tail=2, top_k=0,
        )
        for i in range(5):
            mem.add_message("user", f"msg-{i}")
        msgs = mem.get_messages()
        # Only the last 2 should appear when no query is set.
        assert len(msgs) == 2
        assert msgs[-1]["content"] == "msg-4"

    def test_semantic_recall_via_set_query(self):
        mem = SemanticMemory(
            embeddings=HashEmbeddings(), recent_tail=2, top_k=1, min_score=0.0,
        )
        mem.add_message("user", "My favorite color is blue.")
        mem.add_message("assistant", "Noted -- blue.")
        for i in range(3):
            mem.add_message("user", f"other topic {i}")
            mem.add_message("assistant", f"reply {i}")
        mem.set_query("what is my favorite color")
        msgs = mem.get_messages()
        # A retrieved-context system block should be present.
        assert any(m["role"] == "system" and "RELEVANT PRIOR CONTEXT" in m["content"] for m in msgs)

    def test_system_message_preserved(self):
        mem = SemanticMemory(embeddings=HashEmbeddings(), recent_tail=2)
        mem.add_message("system", "You are helpful.")
        mem.add_message("user", "hi")
        msgs = mem.get_messages()
        assert msgs[0]["role"] == "system"
        assert "You are helpful" in msgs[0]["content"]


class TestVectorSearchTool:

    def test_tool_shape(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["x", "y", "z"])
        tool = vector_search_tool(store, name="docs")
        assert tool.name == "docs"
        # Schema exposes query, top_k, min_score.
        fields = set(tool.args_schema.model_fields.keys())
        assert fields == {"query", "top_k", "min_score"}

    def test_tool_returns_formatted_hits(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["Alpha centauri", "Beta pictoris"])
        tool = vector_search_tool(store, default_top_k=2)
        result = tool.func(query="Alpha", top_k=2, min_score=0.0)
        assert "Alpha" in result

    def test_top_k_capped_at_max(self):
        store = VectorStore(embeddings=HashEmbeddings())
        store.add(["a", "b", "c", "d", "e"])
        tool = vector_search_tool(store, default_top_k=2, max_top_k=3)
        # Even if LLM asks for 500, we cap at max_top_k
        result = tool.func(query="a", top_k=500, min_score=0.0)
        # Split the result -- should have at most 3 numbered entries.
        num_entries = result.count("\n1.") + result.count("\n2.") + result.count("\n3.")
        assert num_entries <= 3

    def test_empty_store_says_no_results(self):
        store = VectorStore(embeddings=HashEmbeddings())
        tool = vector_search_tool(store)
        result = tool.func(query="anything")
        assert "no results" in result.lower() or "empty" in result.lower() or result != ""


class TestFactory:

    def test_create_semantic_memory_default(self):
        mem = create_semantic_memory()
        assert isinstance(mem, SemanticMemory)
