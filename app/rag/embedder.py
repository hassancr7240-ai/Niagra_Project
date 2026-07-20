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
    if settings.ai_provider == "watsonx":
        return await _embed_watsonx(chunks)
    return await _embed_ollama(chunks)


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


async def _embed_ollama(chunks: list[TextChunk]) -> list[dict]:
    """
    Ollama native /api/embed — forces CPU mode via num_gpu=0.
    Uses the native Ollama REST endpoint (not the OpenAI-compat one) so we
    can pass options.num_gpu=0 and bypass the CUDA PTX toolchain crash that
    occurs when the GPU driver version doesn't match the compiled model.
    Sequential batches (no concurrency) — CPU handles one at a time anyway.
    """
    import httpx

    base = (settings.openai_base_url or "http://localhost:11434/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/api/embed"
    model = settings.openai_embedding_model
    batch_size = 10  # small batches — gentler on CPU RAM

    batches = [chunks[i:i + batch_size] for i in range(0, len(chunks), batch_size)]
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=120) as client:
        for batch in batches:
            texts = [c.text for c in batch]
            try:
                resp = await client.post(url, json={
                    "model": model,
                    "input": texts,
                    "options": {"num_gpu": 0},  # force CPU — avoids CUDA crash
                })
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings", [])
                if len(embeddings) == len(batch):
                    results.extend(_chunk_to_dict(batch[j], emb) for j, emb in enumerate(embeddings))
                else:
                    logger.warning("Ollama embedding count mismatch: got %d for %d chunks",
                                   len(embeddings), len(batch))
            except Exception as exc:
                logger.error("Ollama embedding batch failed: %s", exc)

    logger.info("Ollama (CPU) embedded %d/%d chunks in %d batches",
                len(results), len(chunks), len(batches))
    return results


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
