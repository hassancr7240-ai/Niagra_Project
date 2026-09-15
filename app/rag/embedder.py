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
# Hard timeouts — each level independently bounded so asyncio.wait_for is a last resort only
_IBM_BATCH_TIMEOUT   = 12   # IBM 403 (WML unlinked) responds in <1s; 12s is very generous
_IBM_IAM_TIMEOUT     = 10   # IAM token fetch — should complete in <2s on IBM network
_IBM_TOTAL_TIMEOUT   = 25   # entire IBM embedding attempt
_OLLAMA_CHUNK_TIMEOUT = 10  # per-chunk Ollama connect+response (nomic-embed-text on ACI)
_OLLAMA_TOTAL_TIMEOUT = 20  # entire Ollama embedding attempt


async def embed_chunks(chunks: list[TextChunk]) -> list[dict]:
    if not chunks:
        return []

    # IBM first — hard 25s cap so entire function can never block more than 45s
    result: list[dict] = []
    try:
        result = await asyncio.wait_for(_embed_watsonx(chunks), timeout=_IBM_TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("IBM embedding timed out (%ds) — skipping", _IBM_TOTAL_TIMEOUT)
    except Exception as exc:
        logger.warning("IBM embedding failed: %s — skipping", exc)

    if not result and settings.ollama_url:
        logger.info("IBM embedding returned 0 — trying Ollama at %s", settings.ollama_url)
        try:
            result = await asyncio.wait_for(_embed_ollama(chunks), timeout=_OLLAMA_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Ollama embedding timed out (%ds) — skipping", _OLLAMA_TOTAL_TIMEOUT)
        except Exception as exc:
            logger.warning("Ollama embedding failed: %s — skipping", exc)

    logger.info("embed_chunks: %d/%d chunks embedded", len(result), len(chunks))
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
    Each HTTP call is capped at _IBM_BATCH_TIMEOUT so a single stuck call
    never blocks the group.
    """
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    if not settings.watsonx_api_key:
        logger.debug("WATSONX_API_KEY not set — IBM embedding skipped")
        return []

    url = f"{settings.watsonx_url}/ml/v1/text/embeddings?version=2024-03-14"
    batch_size = 25

    try:
        # IAM token fetch — tight 10s cap
        headers = await asyncio.wait_for(
            watsonx_headers(settings.watsonx_api_key),
            timeout=_IBM_IAM_TIMEOUT,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        logger.error("watsonx IAM auth failed/timed out: %s", exc)
        return []

    async def _embed_batch(batch: list[TextChunk], client: httpx.AsyncClient) -> list[dict]:
        payload = {
            "model_id": settings.watsonx_embedding_model,
            "project_id": settings.watsonx_project_id,
            # IBM slate-125m max 512 tokens (~1900 chars). Truncate to avoid 400.
            "inputs": [c.text[:1800] for c in batch],
        }
        try:
            resp = await asyncio.wait_for(
                client.post(url, headers=headers, json=payload),
                timeout=_IBM_BATCH_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                _chunk_to_dict(batch[j], result["embedding"])
                for j, result in enumerate(data.get("results", []))
            ]
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("watsonx embedding batch failed: %s", exc)
            return []

    batches = [chunks[i:i + batch_size] for i in range(0, len(chunks), batch_size)]
    results: list[dict] = []

    # Use short per-call timeout; client-level timeout is a safety net only
    async with httpx.AsyncClient(timeout=httpx.Timeout(_IBM_BATCH_TIMEOUT, connect=5.0)) as client:
        for i in range(0, len(batches), _WATSONX_CONCURRENCY):
            group = batches[i:i + _WATSONX_CONCURRENCY]
            batch_results = await asyncio.gather(
                *[_embed_batch(b, client) for b in group],
                return_exceptions=True,
            )
            for br in batch_results:
                if isinstance(br, list):
                    results.extend(br)

    logger.info("watsonx embedded %d/%d chunks (%d batches)", len(results), len(chunks), len(batches))
    return results


async def _embed_ollama(chunks: list[TextChunk]) -> list[dict]:
    """Ollama embedding fallback — one request per chunk, all concurrent."""
    import httpx
    url = f"{settings.ollama_url}/api/embeddings"

    async def _embed_one(chunk: TextChunk, client: httpx.AsyncClient) -> Optional[dict]:
        try:
            resp = await asyncio.wait_for(
                client.post(url, json={"model": settings.ollama_embedding_model, "prompt": chunk.text}),
                timeout=_OLLAMA_CHUNK_TIMEOUT,
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding", [])
            if emb:
                return _chunk_to_dict(chunk, emb)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("Ollama embedding chunk %s failed: %s", chunk.chunk_id, exc)
        return None

    # connect=5s so unreachable host fails in 5s not 10s
    async with httpx.AsyncClient(timeout=httpx.Timeout(_OLLAMA_CHUNK_TIMEOUT, connect=5.0)) as client:
        raw = await asyncio.gather(*[_embed_one(c, client) for c in chunks], return_exceptions=True)

    results = [r for r in raw if isinstance(r, dict)]
    logger.info("Ollama embedded %d/%d chunks", len(results), len(chunks))
    return results
