"""Generate local embeddings through Ollama."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import time
from datetime import datetime, timezone

from config.settings import OLLAMA_EMBED_URL, OLLAMA_EMBEDDING_MODEL
from agent.request_trace import record_llm_call


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a non-empty batch and validate Ollama's response shape."""
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding inputs must be non-empty strings.")

    started_at = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    try:
        response = httpx.post(
            OLLAMA_EMBED_URL,
            json={"model": OLLAMA_EMBEDDING_MODEL, "input": texts},
            timeout=120.0,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ValueError("Ollama returned an unexpected embedding count.")
        if any(not isinstance(vector, list) or not vector for vector in embeddings):
            raise ValueError("Ollama returned an empty or invalid embedding vector.")
        dimensions = {len(vector) for vector in embeddings}
        if len(dimensions) != 1:
            raise ValueError("Ollama returned inconsistent embedding dimensions.")
    except Exception as error:
        record_llm_call(
            started_at=started_at,
            monotonic_started=monotonic_started,
            model=OLLAMA_EMBEDDING_MODEL,
            prompt=texts,
            error=error,
            metadata={"operation": "embedding"},
        )
        raise
    record_llm_call(
        started_at=started_at,
        monotonic_started=monotonic_started,
        model=OLLAMA_EMBEDDING_MODEL,
        prompt=texts,
        response=embeddings,
        metadata={"operation": "embedding"},
    )
    return embeddings


def embed_text(text: str) -> list[float]:
    """Embed one string using the same endpoint as batch indexing."""
    return embed_texts([text])[0]
