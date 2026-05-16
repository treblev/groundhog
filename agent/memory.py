import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib

import duckdb
import httpx

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"


def _embed(text: str) -> list[float]:
    response = httpx.post(
        OLLAMA_URL,
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["embedding"]


def remember(con: duckdb.DuckDBPyConnection, fact: str) -> str:
    embedding = _embed(fact)
    fact_id = hashlib.sha256(fact.encode()).hexdigest()[:16]
    con.execute(
        """
        INSERT INTO memory (id, fact, embedding)
        VALUES (?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [fact_id, fact, embedding],
    )
    return f"Remembered: {fact}"


def recall(con: duckdb.DuckDBPyConnection, query: str, top_k: int = 3) -> str:
    embedding = _embed(query)
    rows = con.execute(
        """
        SELECT fact, list_cosine_similarity(embedding, ?) AS score
        FROM memory
        ORDER BY score DESC
        LIMIT ?
        """,
        [embedding, top_k],
    ).fetchall()
    if not rows:
        return "No relevant memories found."
    return "\n".join(f"- {row[0]} (score: {row[1]:.3f})" for row in rows)
