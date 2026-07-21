"""Tests for TextSplitter + Document + VectorStore.add_documents."""

from pathlib import Path

import pytest

from agentx_dev import (
    Document, TextSplitter,
    HashEmbeddings, VectorStore,
)


class TestConstructor:

    def test_defaults(self):
        s = TextSplitter()
        assert s.chunk_size == 1000
        assert s.chunk_overlap == 200

    def test_rejects_bad_sizes(self):
        with pytest.raises(ValueError):
            TextSplitter(chunk_size=0)
        with pytest.raises(ValueError):
            TextSplitter(chunk_overlap=-1)
        with pytest.raises(ValueError):
            TextSplitter(chunk_size=100, chunk_overlap=100)   # overlap == size
        with pytest.raises(ValueError):
            TextSplitter(chunk_size=100, chunk_overlap=150)   # overlap > size


class TestSplitText:

    def test_short_text_returns_one_chunk(self):
        s = TextSplitter(chunk_size=100, chunk_overlap=10)
        chunks = s.split_text("hello world")
        assert chunks == ["hello world"]

    def test_empty_text_returns_empty(self):
        assert TextSplitter().split_text("") == []

    def test_paragraph_boundaries_preferred(self):
        # Two paragraphs, each fits under chunk_size; splitter should
        # emit them as separate chunks rather than jamming together.
        text = "First paragraph line one.\n\nSecond paragraph line two."
        s = TextSplitter(chunk_size=30, chunk_overlap=0)
        chunks = s.split_text(text)
        # First para is 25 chars ("First paragraph line one."), fits.
        # Second is 26 chars, fits. Both should exist.
        assert len(chunks) == 2
        assert "First paragraph" in chunks[0]
        assert "Second paragraph" in chunks[1]

    def test_long_text_recursive_split(self):
        # Long text with paragraph breaks -- recursive splitter should
        # find them and produce chunks within (chunk_size + overlap).
        paras = ["This is paragraph number " + str(i) + ".\n" * 3 for i in range(20)]
        text = "\n\n".join(paras)
        s = TextSplitter(chunk_size=200, chunk_overlap=40)
        chunks = s.split_text(text)
        assert len(chunks) > 1
        # Chunks may be up to chunk_size + overlap after boundary
        # preservation + overlap prepend.
        for c in chunks:
            assert len(c) <= 200 + 40, f"chunk too big: {len(c)}"

    def test_overlap_between_adjacent_chunks(self):
        # Force a text with no paragraph breaks so the splitter has to
        # use the char-level fallback -- easier to verify overlap.
        text = "a" * 500 + "b" * 500
        s = TextSplitter(chunk_size=200, chunk_overlap=50, separators=[""])
        chunks = s.split_text(text)
        assert len(chunks) >= 2
        # Adjacent chunks should share ~50 chars.
        overlap = ""
        for i in range(1, min(3, len(chunks))):
            for k in range(50, 10, -5):
                if chunks[i - 1][-k:] == chunks[i][:k]:
                    overlap = chunks[i - 1][-k:]
                    break
        assert overlap, "no measurable overlap between adjacent chunks"

    def test_fixed_char_mode(self):
        text = "x" * 300
        s = TextSplitter(chunk_size=100, chunk_overlap=20, separators=[""])
        chunks = s.split_text(text)
        assert len(chunks) >= 3
        # Standard splitter semantic: chunks may be up to chunk_size +
        # chunk_overlap after the overlap prepend. Boundary preservation
        # matters more than a strict cap.
        for c in chunks:
            assert len(c) <= 100 + 20


class TestSplitDocument:

    def test_propagates_metadata(self):
        doc = Document(text="hello world", metadata={"source": "test.md"})
        chunks = TextSplitter(chunk_size=100).split_document(doc)
        assert len(chunks) == 1
        assert chunks[0].metadata["source"] == "test.md"
        assert chunks[0].metadata["chunk_index"] == 0

    def test_adds_chunk_index(self):
        text = "\n\n".join(f"Paragraph number {i}. " * 5 for i in range(10))
        doc = Document(text=text, metadata={"kind": "policy"})
        chunks = TextSplitter(chunk_size=100, chunk_overlap=10).split_document(doc)
        assert len(chunks) > 1
        for i, c in enumerate(chunks):
            assert c.metadata["chunk_index"] == i
            assert c.metadata["kind"] == "policy"

    def test_split_documents_flat_list(self):
        docs = [
            Document(text="Alpha content.", metadata={"src": "a"}),
            Document(text="Beta content.", metadata={"src": "b"}),
        ]
        chunks = TextSplitter().split_documents(docs)
        assert len(chunks) == 2
        assert {c.metadata["src"] for c in chunks} == {"a", "b"}


class TestSplitFile:

    def test_reads_file_and_adds_source(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("Hello from disk.\n\nSecond paragraph.")
        chunks = TextSplitter(chunk_size=100).split_file(f)
        assert len(chunks) >= 1
        assert all(str(f) in c.metadata["source"] for c in chunks)

    def test_metadata_override(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("Content.")
        chunks = TextSplitter().split_file(f, metadata={"source": "override"})
        assert chunks[0].metadata["source"] == "override"


class TestSplitDirectory:

    def test_walks_directory(self, tmp_path):
        (tmp_path / "a.md").write_text("Alpha content.")
        (tmp_path / "b.md").write_text("Beta content.")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("Gamma content.")
        chunks = TextSplitter().split_directory(tmp_path)
        assert len(chunks) == 3
        sources = {c.metadata["source"] for c in chunks}
        # Sources are relative to the root.
        assert any(s.endswith("a.md") for s in sources)
        assert any(s.endswith("b.md") for s in sources)
        assert any("c.md" in s for s in sources)

    def test_glob_filter(self, tmp_path):
        (tmp_path / "keep.md").write_text("Keep me.")
        (tmp_path / "skip.txt").write_text("Skip me.")
        chunks = TextSplitter().split_directory(tmp_path, glob="*.md")
        assert len(chunks) == 1
        assert chunks[0].text.startswith("Keep")

    def test_requires_directory(self, tmp_path):
        with pytest.raises(ValueError):
            TextSplitter().split_directory(tmp_path / "no_such_dir")


class TestVectorStoreIntegration:

    def test_add_documents_unpacks_text_and_metadata(self):
        docs = [
            Document(text="MVCC is a concurrency approach.", metadata={"src": "db.md"}),
            Document(text="Python has a GIL.", metadata={"src": "py.md"}),
        ]
        store = VectorStore(embeddings=HashEmbeddings())
        ids = store.add_documents(docs)
        assert len(ids) == 2
        assert len(store) == 2
        hits = store.search("Python GIL", top_k=1)
        assert hits[0].metadata["src"] == "py.md"

    def test_full_pipeline(self, tmp_path):
        (tmp_path / "policy.md").write_text(
            "Refunds within 30 days.\n\n"
            "Cancellations effective at period end.\n\n"
            "Data purged after 90 days."
        )
        splitter = TextSplitter(chunk_size=50, chunk_overlap=10)
        store = VectorStore(embeddings=HashEmbeddings())
        docs = splitter.split_directory(tmp_path)
        store.add_documents(docs)
        assert len(store) >= 3
        hits = store.search("refunds", top_k=1)
        assert "Refunds" in hits[0].text or "refund" in hits[0].text.lower()


class TestDocumentDataclass:

    def test_with_metadata_merges(self):
        d = Document(text="x", metadata={"a": 1, "b": 2})
        d2 = d.with_metadata(b=3, c=4)
        assert d.metadata == {"a": 1, "b": 2}       # original untouched
        assert d2.metadata == {"a": 1, "b": 3, "c": 4}
