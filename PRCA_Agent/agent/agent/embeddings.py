"""Local sentence-transformer embeddings — no API keys, $0, 384-dim vectors.

Uses all-MiniLM-L6-v2 which produces 384-dimensional embeddings, matching
the pgvector column dimension defined in the data model.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


@lru_cache
def get_embedder() -> "SentenceTransformer":
    """Return a cached local SentenceTransformer instance (all-MiniLM-L6-v2)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts and return a list of 384-dim float vectors."""
    if not texts:
        return []
    model = get_embedder()
    vectors = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return vectors.tolist()
