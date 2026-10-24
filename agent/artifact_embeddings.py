"""Keep generated-summary vectors on their canonical derived_artifacts rows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import math
from collections.abc import Callable

import duckdb

from agent.embeddings import embed_texts
from config.settings import DB_PATH, OLLAMA_EMBEDDING_MODEL

SUMMARY_TYPES = ("daily_summary", "weekly_review")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _vectors(texts: list[str], embedder: Callable) -> list[list[float]]:
    vectors = embedder(texts)
    if len(vectors) != len(texts):
        raise ValueError("Embedding provider returned an unexpected vector count.")
    if any(
        not vector
        or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
        or not any(vector)
        for vector in vectors
    ):
        raise ValueError("Embedding provider returned an invalid vector.")
    if len({len(vector) for vector in vectors}) > 1:
        raise ValueError("Embedding provider returned inconsistent vector dimensions.")
    return vectors


def _pending(con: duckdb.DuckDBPyConnection, artifact_id: str | None = None) -> list:
    return con.execute(
        """
        SELECT id, content FROM derived_artifacts
        WHERE artifact_type IN (?, ?) AND length(trim(content)) > 0
          AND (embedding IS NULL OR array_length(embedding) = 0
               OR embedding_model IS DISTINCT FROM ?
               OR embedding_content_hash IS DISTINCT FROM sha256(content))
          AND (? IS NULL OR id = ?)
        ORDER BY period_end, id
        """,
        [*SUMMARY_TYPES, OLLAMA_EMBEDDING_MODEL, artifact_id, artifact_id],
    ).fetchall()


def _write(con: duckdb.DuckDBPyConnection, row: tuple, vector: list[float]) -> bool:
    # A concurrent regeneration must never receive the vector of an older text.
    return con.execute(
        """
        UPDATE derived_artifacts
        SET embedding = ?, embedding_model = ?, embedding_content_hash = ?
        WHERE id = ? AND content = ?
        RETURNING id
        """,
        [vector, OLLAMA_EMBEDDING_MODEL, content_hash(row[1]), row[0], row[1]],
    ).fetchone() is not None


def embed_artifact(
    con: duckdb.DuckDBPyConnection,
    artifact_id: str,
    embedder: Callable | None = None,
) -> int:
    """Embed a newly stored summary, reusing an unchanged current vector."""
    rows = _pending(con, artifact_id)
    if not rows:
        return 0
    vector = _vectors([rows[0][1]], embedder or embed_texts)[0]
    return int(_write(con, rows[0], vector))


def sync_artifact_embeddings(
    db_path: Path | str = DB_PATH,
    embedder: Callable | None = None,
    dry_run: bool = False,
    batch_size: int = 32,
) -> dict:
    """Backfill/repair summaries, with Ollama calls outside database connections."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = _pending(con)
        sources = con.execute(
            "SELECT COUNT(*) FROM derived_artifacts WHERE artifact_type IN (?, ?)",
            list(SUMMARY_TYPES),
        ).fetchone()[0]
    finally:
        con.close()

    written = 0
    dimensions = set()
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = _vectors([row[1] for row in batch], embedder or embed_texts)
        dimensions.update(len(vector) for vector in vectors)
        if len(dimensions) > 1:
            raise ValueError("Embedding provider returned inconsistent vector dimensions.")
        if not dry_run:
            con = duckdb.connect(str(db_path))
            try:
                con.execute("BEGIN")
                for row, vector in zip(batch, vectors):
                    written += int(_write(con, row, vector))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()
    return {
        "dry_run": dry_run,
        "sources": sources,
        "pending": len(rows),
        "embedded": written,
        "unchanged": sources - len(rows),
        "skipped_changed": 0 if dry_run else len(rows) - written,
    }


def search_artifacts(
    query: str,
    *,
    db_path: Path | str = DB_PATH,
    top_k: int = 5,
    start_date=None,
    end_date=None,
    embedder: Callable | None = None,
    sync: bool = True,
) -> list[dict]:
    """Return summary text from the same rows whose vectors matched the query."""
    if not query.strip():
        raise ValueError("query must not be empty")
    if sync:
        sync_artifact_embeddings(db_path, embedder=embedder)
    vector = _vectors([query], embedder or embed_texts)[0]
    clauses = [
        "artifact_type IN (?, ?)", "embedding_model = ?",
        "embedding_content_hash = sha256(content)", "array_length(embedding) = ?",
    ]
    parameters = [vector, *SUMMARY_TYPES, OLLAMA_EMBEDDING_MODEL, len(vector)]
    if start_date:
        clauses.append("period_end >= ?")
        parameters.append(start_date)
    if end_date:
        clauses.append("period_end <= ?")
        parameters.append(end_date)
    parameters.append(max(1, min(int(top_k), 10)))
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cursor = con.execute(
            f"""
            SELECT id AS source_id, artifact_type, period_start, period_end, content,
                   list_cosine_similarity(embedding, ?) AS score
            FROM derived_artifacts WHERE {' AND '.join(clauses)}
            ORDER BY score DESC, period_end DESC, id
            LIMIT ?
            """,
            parameters,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        con.close()
