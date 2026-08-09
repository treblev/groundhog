"""Generate local embeddings through Ollama."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from config.settings import OLLAMA_EMBED_URL, OLLAMA_EMBEDDING_MODEL


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a non-empty batch and validate Ollama's response shape."""
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding inputs must be non-empty strings.")

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
    return embeddings


def embed_text(text: str) -> list[float]:
    """Embed one string using the same endpoint as batch indexing."""
    return embed_texts([text])[0]
