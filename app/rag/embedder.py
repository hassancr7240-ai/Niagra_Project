from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import get_settings
from app.rag.chunker import TextChunk

logger = logging.getLogger(__name__)
settings = get_settings()

# Max concurrent batch requests to watsonx cloud API
_WATSONX_CONCURRENCY = 5


async def embed_chunks(chunks: list[TextChunk]) -> list[dict]:
    if not chunks:
        return []
    result = await _embed_watsonx(chunks)
    if not result and settings.ollama_url:
        logger.warning("IBM embedding returned 0 — trying Ollama fallback at %s", settings.ollama_url)
        result = await _embed_ollama(chunks)
    return result


def _chunk_to_dict(chunk: TextChunk, embedding: list[float]) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_file": chunk.source_file,
        "embedding": embedding,
        "chunk_type": getattr(chunk, "chunk_type", "text"),
        "section_heading": getattr(chunk, "section_heading", ""),
        "interval_hint": getattr(chunk, "interval_hint", None),
    }


async def _embed_watsonx(chunks: list[TextChunk]) -> list[dict]:
    """
    IBM watsonx.ai slate-125m-english-rtrvr — concurrent batch embedding.
    Batches 25 texts per request, runs up to _WATSONX_CONCURRENCY in parallel.
    """
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    if not settings.watsonx_api_key:
        logger.error("WATSONX_API_KEY not set — cannot embed")
        return []

    url = f"{settings.watsonx_url}/ml/v1/text/embeddings?version=2024-03-14"
    batch_size = 25

    try:
        headers = await watsonx_headers(settings.watsonx_api_key)
    except Exception as exc:
        logger.error("watsonx IAM auth failed: %s", exc)
        return []

    async def _embed_batch(batch: list[TextChunk], client: httpx.AsyncClient) -> list[dict]:
        payload = {
            "model_id": settings.watsonx_embedding_model,
            "project_id": settings.watsonx_project_id,
            "inputs": [c.text for c in batch],
        }
        try:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return [
                _chunk_to_dict(batch[j], result["embedding"])
                for j, result in enumerate(data.get("results", []))
            ]
        except Exception as exc:
            logger.error("watsonx embedding batch failed: %s", exc)
            return []

    batches = [chunks[i:i + batch_size] for i in range(0, len(chunks), batch_size)]
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(batches), _WATSONX_CONCURRENCY):
            group = batches[i:i + _WATSONX_CONCURRENCY]
            batch_results = await asyncio.gather(*[_embed_batch(b, client) for b in group])
            for br in batch_results:
                results.extend(br)

    logger.info("watsonx embedded %d/%d chunks (%d concurrent batches)",
                len(results), len(chunks), len(batches))
    return results


async def _embed_ollama(chunks: list[TextChunk]) -> list[dict]:
    """Ollama embedding fallback — one request per chunk, all concurrent."""
    import httpx
    url = f"{settings.ollama_url}/api/embeddings"

    async def _embed_one(chunk: TextChunk, client: httpx.AsyncClient) -> Optional[dict]:
        try:
            resp = await client.post(
                url,
                json={"model": settings.ollama_embedding_model, "prompt": chunk.text},
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding", [])
            if emb:
                return _chunk_to_dict(chunk, emb)
        except Exception as exc:
            logger.error("Ollama embedding failed for chunk %s: %s", chunk.chunk_id, exc)
        return None

    results: list[dict] = []
    # Short timeout: if Ollama isn't reachable (common when App Service can't hit ACI VNet IP),
    # fail fast in 10s rather than hanging 120s × N chunks.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        raw = await asyncio.gather(*[_embed_one(c, client) for c in chunks], return_exceptions=True)
    for r in raw:
        if isinstance(r, dict):
            results.append(r)

    logger.info("Ollama embedded %d/%d chunks", len(results), len(chunks))
    return results
