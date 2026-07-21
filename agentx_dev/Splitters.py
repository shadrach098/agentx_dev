"""
Text splitting for RAG ingestion.

``TextSplitter`` chunks raw strings, documents, files, or entire
directories into overlapping chunks of a target size. Recursive by
default: tries paragraph -> line -> sentence -> word -> character
boundaries so chunks land at semantically clean breaks whenever
possible, and fall back gracefully when a run of text has none.

``Document`` is the small transport dataclass tying text and metadata
together. Split methods carry metadata through and add ``chunk_index``
+ ``source`` (when known) automatically.

Together with ``VectorStore.add_documents()``, this replaces the
hand-rolled `while i < len(text): chunks.append(text[i: i+1000])`
loops that used to litter every RAG example.

Usage:

    from agentx_dev import TextSplitter, VectorStore, OpenAIEmbeddings

    splitter = TextSplitter(chunk_size=1000, chunk_overlap=200)
    docs = splitter.split_directory("./data/kb", glob="**/*.md")

    store = VectorStore(embeddings=OpenAIEmbeddings())
    store.add_documents(docs)

Or, split a raw string when you don't have a file:

    chunks = splitter.split_text(long_string)   # -> List[str]

Or, split one document:

    doc = Document(text=..., metadata={"kind": "policy"})
    chunks = splitter.split_document(doc)       # -> List[Document]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


__all__ = ["Document", "TextSplitter"]


# ---------------------------------------------------------------------------
# Document transport
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """A chunk of text plus arbitrary metadata.

    Metadata typically contains provenance (``source``, ``page``,
    ``url``) and post-split fields (``chunk_index``). Splitters
    propagate the original metadata into every emitted chunk and add
    ``chunk_index`` automatically.
    """
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def with_metadata(self, **updates: Any) -> "Document":
        """Return a copy with ``updates`` merged into metadata."""
        new_meta = dict(self.metadata)
        new_meta.update(updates)
        return Document(text=self.text, metadata=new_meta)


# ---------------------------------------------------------------------------
# TextSplitter
# ---------------------------------------------------------------------------


# Default separator hierarchy. The splitter walks this list in order,
# splitting on the first one that produces pieces small enough to fit
# the chunk size. Paragraph boundaries are the highest-quality split
# point in prose; whitespace is the fallback for one-line files.
_DEFAULT_SEPARATORS = ("\n\n", "\n", ". ", " ", "")


class TextSplitter:
    """Chunk text into overlapping windows at semantically clean boundaries.

    Recursive by default: tries ``\\n\\n`` (paragraphs), then ``\\n``
    (lines), then ``. `` (sentences), then `` `` (words), and finally
    character-level as a last resort. Pass ``separators=[""]`` to force
    fixed-size character chunking.

    Args:
        chunk_size: Maximum characters per chunk.
        chunk_overlap: Characters shared between adjacent chunks. Must be
            less than ``chunk_size``. Overlap helps retrieval catch
            passages that span a chunk boundary.
        separators: Ordered separator hierarchy. First that produces
            pieces <= ``chunk_size`` wins for each subrange.
        length_fn: How to measure "size." Defaults to ``len`` (chars);
            pass a tokenizer-length function for token-based splitting.
        keep_separator: When True (default), the separator character
            stays at the start of each new chunk so the boundary reads
            naturally; helps sentence-level chunks preserve their period.
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: Optional[int] = None,
        *,
        separators: Sequence[str] = _DEFAULT_SEPARATORS,
        length_fn=len,
        keep_separator: bool = True,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        # Default overlap scales with chunk_size so passing chunk_size=100
        # without specifying overlap doesn't blow up on the size-vs-overlap
        # invariant. 20% is a reasonable RAG default.
        if chunk_overlap is None:
            chunk_overlap = min(200, chunk_size // 5)
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be non-negative")
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) must be smaller than "
                f"chunk_size ({chunk_size})"
            )
        if not separators:
            raise ValueError("separators must contain at least one entry")
        self.chunk_size = int(chunk_size)
        self.chunk_overlap = int(chunk_overlap)
        self._separators = tuple(separators)
        self._length = length_fn
        self._keep_separator = bool(keep_separator)

    # ---- public split entry points -----------------------------------

    def split_text(self, text: str) -> List[str]:
        """Split a raw string. Returns the list of chunk strings."""
        if not text:
            return []
        pieces = self._recursive_split(text, list(self._separators))
        return self._merge_with_overlap(pieces)

    def split_document(self, doc: Document) -> List[Document]:
        """Split one Document, propagating metadata and adding ``chunk_index``."""
        chunks = self.split_text(doc.text)
        return [
            Document(text=c, metadata={**doc.metadata, "chunk_index": i})
            for i, c in enumerate(chunks)
        ]

    def split_documents(self, docs: Iterable[Document]) -> List[Document]:
        """Split many documents, flat-list the result."""
        out: List[Document] = []
        for d in docs:
            out.extend(self.split_document(d))
        return out

    def split_file(
        self,
        path: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        encoding: str = "utf-8",
    ) -> List[Document]:
        """Read a file and split its contents. Adds ``source`` metadata
        (relative to cwd) unless already present in ``metadata``."""
        p = Path(path)
        text = p.read_text(encoding=encoding)
        meta = dict(metadata or {})
        meta.setdefault("source", str(p))
        return self.split_document(Document(text=text, metadata=meta))

    def split_directory(
        self,
        path: Any,
        *,
        glob: str = "**/*.md",
        encoding: str = "utf-8",
        base_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Document]:
        """Walk a directory, read every file matching ``glob``, split all.

        ``source`` metadata is set to the path RELATIVE to ``path`` so
        the same corpus lands with the same source keys regardless of
        cwd.
        """
        root = Path(path)
        if not root.is_dir():
            raise ValueError(f"{root} is not a directory")
        out: List[Document] = []
        for f in sorted(root.rglob(glob)):
            if not f.is_file():
                continue
            meta = dict(base_metadata or {})
            meta.setdefault("source", str(f.relative_to(root)))
            try:
                text = f.read_text(encoding=encoding)
            except (UnicodeDecodeError, PermissionError):
                continue
            out.extend(self.split_document(Document(text=text, metadata=meta)))
        return out

    # ---- internals ---------------------------------------------------

    def _recursive_split(self, text: str, separators: List[str]) -> List[str]:
        """Find the first separator that produces manageable pieces; if
        no separator qualifies, hard-chunk on characters."""
        if self._length(text) <= self.chunk_size:
            return [text] if text else []

        # Walk the separator list. If a piece produced by the current
        # separator is still too big, recurse into it with the next-
        # lower separators.
        while separators:
            sep = separators[0]
            remaining = separators[1:]
            if sep == "":
                # Character-level fallback.
                return self._hard_split_by_chars(text)

            splits = self._split_on(text, sep)
            # If splitting produced only one piece equal to the input,
            # this separator doesn't help; move to the next one.
            if len(splits) <= 1:
                separators = remaining
                continue

            good: List[str] = []
            for piece in splits:
                if self._length(piece) <= self.chunk_size:
                    good.append(piece)
                else:
                    good.extend(self._recursive_split(piece, remaining or [""]))
            return good

        return self._hard_split_by_chars(text)

    def _split_on(self, text: str, sep: str) -> List[str]:
        """Split on ``sep``. When ``keep_separator``, prepend the sep to
        each piece after the first so boundary chars aren't lost."""
        if sep == "":
            return list(text)
        parts = text.split(sep)
        if not self._keep_separator or len(parts) <= 1:
            return parts
        out = [parts[0]]
        for p in parts[1:]:
            out.append(sep + p)
        # Empty leading piece is possible when text starts with sep.
        return [p for p in out if p != ""] or parts

    def _hard_split_by_chars(self, text: str) -> List[str]:
        """Character-level fallback for text with no useful separators
        (long single-word runs, tokenizer strings, etc.)."""
        size = self.chunk_size
        step = max(1, size - self.chunk_overlap)
        return [text[i: i + size] for i in range(0, self._length(text), step)]

    def _merge_with_overlap(self, pieces: List[str]) -> List[str]:
        """Greedily pack pieces into chunks up to ``chunk_size``, then
        prepend ``chunk_overlap`` characters from the previous chunk's
        tail so adjacent chunks share context."""
        if not pieces:
            return []

        chunks: List[str] = []
        buf: List[str] = []
        buf_len = 0
        overlap = self.chunk_overlap

        for piece in pieces:
            p_len = self._length(piece)
            if buf_len + p_len <= self.chunk_size:
                buf.append(piece)
                buf_len += p_len
                continue

            # Emit the current buffer as a chunk.
            if buf:
                chunk = "".join(buf)
                chunks.append(chunk)
                # Seed the next buffer with the last `overlap` characters.
                tail = chunk[-overlap:] if overlap and self._length(chunk) > overlap else ""
                buf = [tail] if tail else []
                buf_len = self._length(tail)

            # Piece itself may exceed chunk_size (long single word). If so,
            # hard-chunk it.
            if p_len > self.chunk_size:
                for sub in self._hard_split_by_chars(piece):
                    chunks.append(sub)
                buf = []
                buf_len = 0
            else:
                buf.append(piece)
                buf_len += p_len

        if buf:
            chunks.append("".join(buf))

        # Trim leading/trailing whitespace on each chunk since separator-
        # kept splits often produce boundary whitespace.
        return [c.strip() for c in chunks if c.strip()]
