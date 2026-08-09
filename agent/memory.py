import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
from collections.abc import Callable

import duckdb
from agent.embeddings import embed_text, embed_texts
from config.settings import DB_PATH, OLLAMA_EMBEDDING_MODEL


def _embed(text: str) -> list[float]:
    return embed_text(text)


def remember(con: duckdb.DuckDBPyConnection, fact: str) -> str:
    embedding = _embed(fact)
    fact_id = hashlib.sha256(fact.encode()).hexdigest()[:16]
    con.execute(
        """
        INSERT INTO memory (id, fact, embedding, embedding_model)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            fact = excluded.fact,
            embedding = excluded.embedding,
            embedding_model = excluded.embedding_model
        """,
        [fact_id, fact, embedding, OLLAMA_EMBEDDING_MODEL],
    )
    return f"Remembered: {fact}"


def recall(con: duckdb.DuckDBPyConnection, query: str, top_k: int = 3) -> str:
    embedding = _embed(query)
    rows = con.execute(
        """
        SELECT fact, list_cosine_similarity(embedding, ?) AS score
        FROM memory
        WHERE embedding_model = ?
        ORDER BY score DESC
        LIMIT ?
        """,
        [embedding, OLLAMA_EMBEDDING_MODEL, top_k],
    ).fetchall()
    if not rows:
        return "No relevant memories found."
    return "\n".join(f"- {row[0]} (score: {row[1]:.3f})" for row in rows)


def sync_memory_embeddings(
    db_path: Path | str = DB_PATH,
    embedder: Callable[[list[str]], list[list[float]]] = embed_texts,
    dry_run: bool = False,
) -> dict:
    """Re-embed legacy memories with the configured model without mixing dimensions."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT id, fact, embedding_model FROM memory ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    pending = [row for row in rows if row[2] != OLLAMA_EMBEDDING_MODEL]
    vectors = embedder([row[1] for row in pending]) if pending else []
    if len(vectors) != len(pending):
        raise ValueError("Embedding provider returned an unexpected vector count.")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) > 1:
        raise ValueError("Embedding provider returned inconsistent vector dimensions.")

    if pending and not dry_run:
        con = duckdb.connect(str(db_path))
        try:
            con.execute("BEGIN")
            for row, vector in zip(pending, vectors):
                con.execute(
                    """
                    UPDATE memory
                    SET embedding = ?, embedding_model = ?
                    WHERE id = ?
                    """,
                    [vector, OLLAMA_EMBEDDING_MODEL, row[0]],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    return {
        "dry_run": dry_run,
        "facts": len(rows),
        "reembedded": len(pending),
        "unchanged": len(rows) - len(pending),
        "dimensions": next(iter(dimensions), None),
        "writes": 0 if dry_run else len(pending),
    }
