# Vector store adapters

The in-memory `VectorStore` in `agentx_dev.Embeddings` fits a few thousand
chunks and one process. When you outgrow that, drop in one of these
adapters -- they present the **same public shape** (`add`, `search`,
`delete`, `clear`, `__len__`) so `vector_search_tool` and `SemanticMemory`
accept them without any other code changes.

Adapters ship in `agentx_dev.VectorStores`:

| Adapter | Backing store | Extra install |
|---|---|---|
| `ChromaVectorStore` | Chroma (local persistent or client/server) | `pip install chromadb` |
| `QdrantVectorStore` | Qdrant (local or remote) | `pip install qdrant-client` |
| `PgVectorStore` | Postgres + pgvector | `pip install "psycopg[binary]"` (or `psycopg2-binary`) |

## Chroma

```python
from agentx_dev import OpenAIEmbeddings, vector_search_tool
from agentx_dev.VectorStores import ChromaVectorStore

store = ChromaVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="my_docs",
    persist_directory="./.chroma",   # None = in-memory only
)
store.add(["fact one", "fact two"], metadata=[{"src": "a"}, {"src": "b"}])
tool = vector_search_tool(store)
```

Cosine distance by default (matches the in-memory `VectorStore`). Pass
`distance="l2"` or `distance="ip"` to change.

## Qdrant

```python
from agentx_dev import OpenAIEmbeddings
from agentx_dev.VectorStores import QdrantVectorStore

# Local, file-backed:
store = QdrantVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="my_docs",
    location="./.qdrant",           # or ":memory:" for ephemeral
)

# Or remote:
store = QdrantVectorStore(
    embeddings=OpenAIEmbeddings(),
    collection_name="my_docs",
    url="https://<region>.qdrant.io:6333",
    api_key=os.environ["QDRANT_API_KEY"],
)
```

Qdrant requires UUID or int point IDs -- the adapter hashes your string
ids via UUID5 in a stable namespace so `ids=["doc_A"]` remains
consistent across calls.

## Postgres + pgvector

```python
from agentx_dev import OpenAIEmbeddings
from agentx_dev.VectorStores import PgVectorStore

store = PgVectorStore(
    embeddings=OpenAIEmbeddings(),
    dsn="postgresql://user:pass@localhost:5432/mydb",
    table="agentx_docs",           # auto-quoted; alphanumeric + underscore
    create_table=True,             # creates table + ivfflat index if missing
)
```

Requires the `vector` extension:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

An `ivfflat` index with 100 lists is created on the embedding column
for fast approximate nearest-neighbor search. Cosine distance
(`<=>`); similarity returned as `1 - distance`.

## Interchangeability

All adapters + the in-memory `VectorStore` implement the same interface:

```python
store.add(texts, ids=None, metadata=None) -> list[str]
store.search(query, top_k=5, min_score=0.0) -> list[VectorHit]
store.delete(ids) -> int
store.clear()
len(store)
store.embeddings   # the Embeddings backend
```

So a `vector_search_tool(store)` call works with any of them:

```python
# Same tool, four different backing stores:
from agentx_dev import VectorStore, HashEmbeddings, vector_search_tool
from agentx_dev.VectorStores import ChromaVectorStore, QdrantVectorStore, PgVectorStore

for store in [
    VectorStore(HashEmbeddings()),
    ChromaVectorStore(embeddings=HashEmbeddings()),
    QdrantVectorStore(embeddings=HashEmbeddings(), location=":memory:"),
    # PgVectorStore(embeddings=HashEmbeddings(), dsn=...),
]:
    tool = vector_search_tool(store)
    ...   # same runner code from here on
```

## Choosing a backend

| You have | Use |
|---|---|
| < 10k chunks, single process | `VectorStore` (in-memory) |
| < 100k chunks, disk persistence | `ChromaVectorStore(persist_directory=...)` |
| Multi-process or remote store | `QdrantVectorStore(url=...)` |
| Already running Postgres | `PgVectorStore(dsn=...)` -- reuse your existing DB |
| Millions of chunks + hybrid search | Use the Qdrant/Chroma SDK directly; the adapter is for the base case |

## Persistence

- **In-memory `VectorStore`** -- `save(path)` writes JSON; `load(path,
  embeddings=)` reads it. Doesn't scale but is dead simple.
- **Chroma** -- pass `persist_directory=` at construction. Chroma
  handles persistence.
- **Qdrant** -- pass `location="./path"` for local file-backed; use
  `url=` for remote (persistence is Qdrant's problem).
- **Pgvector** -- Postgres persists everything transparently.

## Reusing the embeddings backend

`SemanticMemory` reads `store.embeddings` so the memory can share the
exact embedding backend the RAG store uses -- essential for dim
consistency:

```python
store = ChromaVectorStore(embeddings=OpenAIEmbeddings(), ...)
memory = SemanticMemory(embeddings=store.embeddings, recent_tail=4, top_k=3)
```

## Migration between backends

The `save`/`load` shape of `VectorStore` is not portable to Chroma /
Qdrant / pgvector. To migrate:

```python
from agentx_dev import VectorStore, OpenAIEmbeddings
from agentx_dev.VectorStores import ChromaVectorStore

old = VectorStore.load("./data/kb.json", embeddings=OpenAIEmbeddings())
new = ChromaVectorStore(embeddings=old.embeddings, persist_directory="./.chroma")
# Re-adding replays the embeddings (no re-embed if same backend).
new.add(old._texts, ids=old._ids, metadata=old._metadata)
```
