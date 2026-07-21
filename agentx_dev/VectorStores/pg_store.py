"""Postgres + pgvector VectorStore adapter.

Usage:

    from agentx_dev import OpenAIEmbeddings
    from agentx_dev.VectorStores import PgVectorStore

    store = PgVectorStore(
        embeddings=OpenAIEmbeddings(),
        dsn="postgresql://user:pass@localhost:5432/mydb",
        table="agentx_docs",
    )
    store.add(["fact one", "fact two"])

Requires the pgvector extension on the database:

    CREATE EXTENSION IF NOT EXISTS vector;

And psycopg (v3, preferred) or psycopg2 installed:

    pip install "psycopg[binary]"
    # or
    pip install psycopg2-binary

The adapter uses cosine distance (``<=>`` operator in pgvector) and
returns similarity = 1 - distance to stay consistent with the in-memory
``VectorStore``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from agentx_dev.Embeddings import Embeddings, VectorHit


def _load_psycopg():
    """Return (module, kind) where kind is 'v3' or 'v2'. Raises if neither
    is installed."""
    try:
        import psycopg
        return psycopg, "v3"
    except ImportError:
        pass
    try:
        import psycopg2 as psycopg
        return psycopg, "v2"
    except ImportError as e:
        raise ImportError(
            "PgVectorStore requires psycopg (v3, preferred) or psycopg2. "
            "Install one of: pip install 'psycopg[binary]' -- OR -- "
            "pip install psycopg2-binary"
        ) from e


class PgVectorStore:
    """Adapter for Postgres + pgvector. Same public shape as ``VectorStore``."""

    def __init__(
        self,
        embeddings: Embeddings,
        *,
        dsn: str,
        table: str = "agentx_docs",
        create_table: bool = True,
    ):
        """
        Args:
            embeddings: Any ``Embeddings`` instance.
            dsn: Postgres connection string.
            table: Table name to read/write. Auto-quoted.
            create_table: If True and the table doesn't exist, create it
                (requires ``CREATE`` grant on the schema).
        """
        psycopg, kind = _load_psycopg()
        self._psycopg = psycopg
        self._kind = kind
        self._embeddings = embeddings
        self._table = self._quote_ident(table)
        self._conn = psycopg.connect(dsn)
        self._conn.autocommit = True

        # Probe embedding dimension so the table has the right vector size.
        dim = getattr(embeddings, "dim", None)
        if not dim:
            dim = len(embeddings.embed_one("."))
            embeddings.dim = dim
        self._dim = int(dim)

        if create_table:
            self._create_table_if_needed()

    @staticmethod
    def _quote_ident(name: str) -> str:
        """Belt-and-suspenders identifier quote (letters/digits/underscore only)."""
        clean = "".join(c for c in name if c.isalnum() or c == "_")
        if not clean or clean != name:
            raise ValueError(
                f"table name {name!r} must be alphanumeric + underscore only "
                "(no dots, quotes, or spaces)"
            )
        return f'"{clean}"'

    def _create_table_if_needed(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id       TEXT PRIMARY KEY,
                    text     TEXT NOT NULL,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    embedding vector({self._dim})
                )
            """)
            # Create an ivfflat index for cosine distance (fast approximate NN).
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {self._table[1:-1]}_embedding_idx
                ON {self._table} USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)

    @property
    def embeddings(self) -> Embeddings:
        return self._embeddings

    def __len__(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._table}")
            (n,) = cur.fetchone()
            return int(n)

    @staticmethod
    def _vec_literal(v: Sequence[float]) -> str:
        """Format a Python list as a pgvector literal string: '[0.1, 0.2, ...]'."""
        return "[" + ",".join(f"{float(x):.8f}" for x in v) + "]"

    def add(
        self,
        texts: Sequence[str],
        *,
        ids: Optional[Sequence[str]] = None,
        metadata: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[str]:
        if not texts:
            return []
        import json
        texts = list(texts)
        if ids is None:
            base = len(self)
            ids = [f"doc_{base + i}" for i in range(len(texts))]
        else:
            ids = [str(x) for x in ids]
        metas = (
            list(metadata) if metadata is not None
            else [{} for _ in texts]
        )
        vectors = self._embeddings.embed(texts)

        rows = [
            (ids[i], texts[i], json.dumps(dict(metas[i] or {})), self._vec_literal(vectors[i]))
            for i in range(len(texts))
        ]
        with self._conn.cursor() as cur:
            # ON CONFLICT (id) DO UPDATE lets add() upsert same as VectorStore.
            cur.executemany(
                f"""
                INSERT INTO {self._table} (id, text, metadata, embedding)
                VALUES (%s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (id) DO UPDATE
                    SET text = EXCLUDED.text,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding
                """,
                rows,
            )
        return list(ids)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> List[VectorHit]:
        if top_k <= 0:
            return []
        qvec = self._vec_literal(self._embeddings.embed_one(query))
        with self._conn.cursor() as cur:
            # Cosine distance <=> in pgvector; similarity = 1 - distance.
            cur.execute(
                f"""
                SELECT id, text, metadata, 1 - (embedding <=> %s::vector) AS score
                FROM {self._table}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (qvec, qvec, top_k),
            )
            rows = cur.fetchall()
        hits: List[VectorHit] = []
        for row in rows:
            id_, text, meta, score = row
            if score < min_score:
                continue
            hits.append(VectorHit(
                id=str(id_),
                text=str(text),
                score=float(score),
                metadata=dict(meta or {}),
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
        ids = list(ids)
        if not ids:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self._table} WHERE id = ANY(%s)",
                (ids,),
            )
            return cur.rowcount or 0

    def clear(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"TRUNCATE {self._table}")

    def close(self) -> None:
        self._conn.close()
