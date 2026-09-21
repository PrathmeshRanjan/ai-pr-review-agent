# backend/memory/embedder.py
#
# Google Gemini embedding client — the bridge between raw text and vector space.
#
# MODEL CHOICE: gemini-embedding-001
# (From RAG-Architecture.md wiki, "Dense Embeddings"):
#   Dense embeddings capture semantic understanding for code semantics.
#   Default output dimensionality: 768 dimensions.
#
# CHUNKING STRATEGY: chunk by FILE, not by token count
#   For code: a FILE is the natural structural unit.
#   If a file is very large (>8000 chars), we truncate from the end
#   to preserve semantically dense headers/signatures.
#
# INPUT LENGTH GUARD: 8000 chars
#   We cap at 8000 chars to stay safely within input constraints.
#
# GRACEFUL DEGRADATION:
#   Catches external API errors and raises EmbeddingError so context_retriever
#   can gracefully fall back without crashing the review workflow.

import logging
from typing import Any

import httpx

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum input length for embedding.
MAX_EMBED_CHARS = 8_000

# Embedding vector dimensionality for gemini-embedding-001.
EMBEDDING_DIMENSIONS = 768


class EmbeddingError(Exception):
    """
    Raised when an embedding API call fails.

    Callers (specifically context_retriever.py) catch this and return ""
    (graceful degradation: no RAG context, review still runs).
    """
    pass


# ---------------------------------------------------------------------------
# Core embedding functions
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float]:
    """
    Embeds a single text string into a 768-dimensional vector using gemini-embedding-001.

    Args:
        text: The text to embed. Will be truncated if over 8000 chars.

    Returns:
        List of 768 floats (the embedding vector).

    Raises:
        EmbeddingError: on any API failure. Caller handles gracefully.
    """
    if not text or not text.strip():
        logger.debug("embed_text | empty_text | returning zero vector")
        return [0.0] * EMBEDDING_DIMENSIONS

    # Truncate to MAX_EMBED_CHARS if needed
    original_len = len(text)
    if original_len > MAX_EMBED_CHARS:
        text = text[:MAX_EMBED_CHARS]
        logger.debug(
            "embed_text | truncated | original_chars=%d truncated_to=%d",
            original_len, MAX_EMBED_CHARS,
        )

    cfg = get_settings()
    api_key = cfg.google_api_key
    model = cfg.google_embedding_model or "gemini-embedding-001"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={api_key}"
    payload: dict[str, Any] = {
        "model": f"models/{model}",
        "content": {
            "parts": [{"text": text}],
        },
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        vector = data.get("embedding", {}).get("values", [])
        if not vector:
            raise EmbeddingError(f"No embedding values returned in response: {data}")

        logger.debug(
            "embed_text | success | model=%s dims=%d input_chars=%d",
            model, len(vector), len(text),
        )
        return vector

    except Exception as e:
        raise EmbeddingError(
            f"Google Gemini embedding call failed: {type(e).__name__}: {e}"
        ) from e


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embeds a list of texts in a batch API call using gemini-embedding-001.

    Args:
        texts: List of strings to embed. Empty list returns [].

    Returns:
        List of 768-dimensional vectors, one per input text.
        Order is preserved: result[i] corresponds to texts[i].

    Raises:
        EmbeddingError: on any API failure.
    """
    if not texts:
        return []

    truncated_texts: list[str] = []
    empty_indices: set[int] = set()

    for i, text in enumerate(texts):
        if not text or not text.strip():
            truncated_texts.append("")
            empty_indices.add(i)
        elif len(text) > MAX_EMBED_CHARS:
            truncated_texts.append(text[:MAX_EMBED_CHARS])
        else:
            truncated_texts.append(text)

    non_empty = [(i, t) for i, t in enumerate(truncated_texts) if i not in empty_indices]

    if not non_empty:
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    cfg = get_settings()
    api_key = cfg.google_api_key
    model = cfg.google_embedding_model or "gemini-embedding-001"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents?key={api_key}"
    requests_payload = [
        {
            "model": f"models/{model}",
            "content": {"parts": [{"text": t}]},
            "outputDimensionality": EMBEDDING_DIMENSIONS,
        }
        for _, t in non_empty
    ]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json={"requests": requests_payload})
            resp.raise_for_status()
            data = resp.json()

        embeddings_data = data.get("embeddings", [])
        if len(embeddings_data) != len(non_empty):
            raise EmbeddingError(
                f"Batch embedding count mismatch: expected {len(non_empty)}, got {len(embeddings_data)}"
            )

        results: list[list[float]] = [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]
        for (position, _), emb_obj in zip(non_empty, embeddings_data):
            results[position] = emb_obj.get("values", [0.0] * EMBEDDING_DIMENSIONS)

        logger.debug(
            "embed_batch | success | model=%s batch_size=%d",
            model, len(non_empty),
        )
        return results

    except Exception as e:
        raise EmbeddingError(
            f"Google Gemini batch embedding call failed: {type(e).__name__}: {e}"
        ) from e
