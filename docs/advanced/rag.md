# RAG with vector_search

RAG (retrieval-augmented generation) lets an agent query a private
knowledge base at runtime instead of stuffing all the docs into the
system prompt.

The framework ships (3.1):

- **`Embeddings`** — abstract base + `OpenAIEmbeddings` + `HashEmbeddings`
  (zero-dep fallback).
- **`VectorStore`** — in-memory cosine similarity search, save/load to
  JSON, numpy-accelerated when available.
- **`vector_search_tool()`** — a factory that hands the agent a real
  tool it can call.

No hard dependency on Chroma / Qdrant / pgvector. Small enough to be
useful for the "few thousand chunks + one agent" case that covers most
prototypes and small production apps.

## Build a store

```python
from agentx_dev import VectorStore, HashEmbeddings, OpenAIEmbeddings

# Zero-dep (deterministic, offline):
store = VectorStore(embeddings=HashEmbeddings(dim=256))

# Or with real embeddings (needs OPENAI_API_KEY):
store = VectorStore(embeddings=OpenAIEmbeddings(model="text-embedding-3-small"))

store.add([
    "Postgres uses MVCC so readers never block writers.",
    "SQLite stores an entire database in one file.",
    "Redis is an in-memory data-structure store.",
])
```

Return values from `.add(...)` are the ids assigned (auto-generated
`doc_0`, `doc_1`, ...; pass `ids=[...]` to control).

## Search

```python
hits = store.search("how does postgres handle concurrency?", top_k=2)
for hit in hits:
    print(f"[{hit.id} score={hit.score:.3f}] {hit.text}")
```

`hit` fields: `id`, `text`, `score` (cosine similarity in `[-1, 1]`),
`metadata`.

Filter weak matches:

```python
hits = store.search(query, top_k=5, min_score=0.5)   # OpenAI-ish threshold
hits = store.search(query, top_k=5, min_score=0.15)  # HashEmbeddings-ish threshold
```

## Persist

```python
store.save("./data/kb.json")
```

Rehydrate later — same embeddings backend, or dimension mismatch will
raise:

```python
from agentx_dev import VectorStore, OpenAIEmbeddings

store = VectorStore.load("./data/kb.json", embeddings=OpenAIEmbeddings())
```

## Give the agent a search tool

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    VectorStore, HashEmbeddings, vector_search_tool,
)

store = VectorStore(embeddings=HashEmbeddings())
store.add(["Postgres uses MVCC...", "SQLite is single-file...", ...])

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[vector_search_tool(store, name="docs", default_top_k=3)],
    permissions=Permissions.read_only(["./"]),
)

result = runner.invoke("How does Postgres handle concurrent writes?")
```

**Factory parameters:**

| Param | Default | Purpose |
|---|---|---|
| `store` | required | The `VectorStore` to query |
| `name` | `"vector_search"` | Tool name the LLM sees |
| `default_top_k` | 5 | Fallback when the LLM doesn't specify |
| `max_top_k` | 20 | Hard cap the LLM can't exceed |
| `description` | auto | Override to hint at what's in the store |

Hint at the store contents in `description` for better tool selection:

```python
vector_search_tool(
    store,
    name="policy_docs",
    description="Search internal HR policy docs for compliance questions.",
)
```

## Ingesting a real corpus — `TextSplitter`

The framework ships a recursive text splitter so you don't have to
hand-roll the chunk loop. It walks paragraph → line → sentence → word
→ char boundaries and picks the best available split point:

```python
from agentx_dev import TextSplitter, Document, VectorStore, OpenAIEmbeddings

splitter = TextSplitter(chunk_size=1000, chunk_overlap=200)

# Simplest: one file
docs = splitter.split_file("./docs/x.md")

# Walk a directory
docs = splitter.split_directory("./docs", glob="**/*.md")

# Split arbitrary strings you already have in memory
docs = splitter.split_documents([
    Document(text=body, metadata={"url": url})
    for url, body in page_texts.items()
])

# Ingest into a store
store = VectorStore(embeddings=OpenAIEmbeddings())
store.add_documents(docs)
store.save("./data/kb.json")
```

Every emitted `Document` carries the original metadata plus a
`chunk_index` field, so retrieval hits can be traced back to the
right piece of the source.

**Splitter parameters:**

| Param | Default | Purpose |
|---|---|---|
| `chunk_size` | `1000` | Max characters per chunk |
| `chunk_overlap` | `min(200, size/5)` | Chars shared between adjacent chunks |
| `separators` | `["\n\n", "\n", ". ", " ", ""]` | Boundary hierarchy, tried in order |
| `length_fn` | `len` | Swap for a tokenizer's length function (`tiktoken`) |
| `keep_separator` | `True` | Keep the delimiter at chunk boundaries so periods survive |

**Custom strategies:**

- **Fixed-char** — pass `separators=[""]` for pure character windowing.
- **Token-length** — pass `length_fn=lambda s: len(tiktoken_encode(s))`.
- **Markdown-heading-aware** — pass `separators=["\n## ", "\n### ", "\n\n", ...]`
  so chunks land at section boundaries.

`Document` is a `@dataclass` (`text`, `metadata`); any dict-like
object with `.text` + `.metadata` attributes works with
`store.add_documents()` too, so you can bring your own doc type.

## Numpy acceleration

The store auto-detects numpy at import. When present, cosine similarity
runs as a single `mat @ q` dot product for the whole corpus — fast up
to hundreds of thousands of vectors on a laptop. Pure-python fallback
kicks in without numpy; slower but works everywhere.

## Delete / clear / overwrite

```python
store.delete(["snippet_5", "snippet_6"])   # by id
store.clear()                              # empty
store.add(["updated content"], ids=["snippet_5"])   # overwrite
```

`add` with an existing id replaces the vector.

## When to graduate to a real vector DB

`VectorStore` fits in a single process's RAM. Reach for
Chroma / Qdrant / Weaviate / pgvector when:

- You have > 100k chunks.
- You need concurrent multi-writer semantics.
- You want hybrid keyword + vector search.
- You want on-disk incremental indexing.

The `Embeddings` interface transfers cleanly to any of them — write a
thin adapter that implements `.embed(texts) -> list[list[float]]` and
plug into their SDK's `add` / `query` methods.

## Runnable example

See `examples/v3_1_comprehensive_demo.py` for a full multi-agent
pipeline where the researcher agent queries a `VectorStore` via
`vector_search_tool`, in parallel via `bind_tools_natively=True`.
