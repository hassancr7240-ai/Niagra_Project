from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import get_settings
from app.rag.chunker import TextChunk

logger = logging.getLogger(__name__)
settings = get_settings()


async def embed_chunks(chunks: list[TextChunk]) -> list[dict]:
    """
    Generate embeddings for text chunks.
    Returns list of dicts with chunk metadata + embedding vector.
    """
    if not chunks:
        return []

    if settings.ai_provider == "watsonx":
        return await _embed_watsonx(chunks)
    return await _embed_openai(chunks)


async def _embed_openai(chunks: list[TextChunk]) -> list[dict]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    # The `dimensions` parameter is an OpenAI-specific extension (text-embedding-3-*).
    # OpenAI-compatible servers like Ollama reject it, so only send it against the
    # real OpenAI endpoint.
    using_openai_dotcom = not settings.openai_base_url
    results = []
    batch_size = 50

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.text for c in batch]

        try:
            kwargs = {"model": settings.openai_embedding_model, "input": texts}
            if using_openai_dotcom:
                kwargs["dimensions"] = settings.openai_embedding_dims
            response = await client.embeddings.create(**kwargs)
            for j, embedding_data in enumerate(response.data):
                chunk = batch[j]
                results.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "text": chunk.text,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                        "source_file": chunk.source_file,
                        "embedding": embedding_data.embedding,
                    }
                )
        except Exception as exc:
            logger.error("OpenAI embedding failed for batch %d: %s", i, exc)

    logger.info("Embedded %d/%d chunks", len(results), len(chunks))
    return results


async def _embed_watsonx(chunks: list[TextChunk]) -> list[dict]:
    """
    IBM watsonx.ai embedding via REST API.
    Uses IAM token auth (api_key → IAM exchange). Batches 25 texts per request.
    Model: ibm/slate-125m-english-rtrvr → 1024-dim vectors.
    """
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    if not settings.watsonx_api_key:
        logger.error("WATSONX_API_KEY not set — cannot embed")
        return []

    url = f"{settings.watsonx_url}/ml/v1/text/embeddings?version=2024-03-14"
    results = []
    batch_size = 25  # watsonx max inputs per call

    try:
        headers = await watsonx_headers(settings.watsonx_api_key)
    except Exception as exc:
        logger.error("watsonx IAM auth failed: %s", exc)
        return []

    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            payload = {
                "model_id": settings.watsonx_embedding_model,
                "project_id": settings.watsonx_project_id,
                "inputs": [c.text for c in batch],
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                for j, result in enumerate(data.get("results", [])):
                    chunk = batch[j]
                    results.append(
                        {
                            "chunk_id": chunk.chunk_id,
                            "text": chunk.text,
                            "page_start": chunk.page_start,
                            "page_end": chunk.page_end,
                            "source_file": chunk.source_file,
                            "embedding": result["embedding"],
                        }
                    )
            except Exception as exc:
                logger.error("watsonx embedding failed for batch %d: %s", i, exc)

    logger.info("watsonx embedded %d/%d chunks", len(results), len(chunks))
    return results
