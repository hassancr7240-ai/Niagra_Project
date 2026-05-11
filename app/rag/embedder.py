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

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    results = []
    batch_size = 50

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.text for c in batch]

        try:
            response = await client.embeddings.create(
                model=settings.openai_embedding_model,
                input=texts,
                dimensions=settings.openai_embedding_dims,
            )
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
    """IBM watsonx.ai embedding via REST API."""
    import httpx

    url = f"{settings.watsonx_url}/ml/v1/text/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.watsonx_api_key}",
        "Content-Type": "application/json",
    }
    results = []

    async with httpx.AsyncClient(timeout=60) as client:
        for chunk in chunks:
            payload = {
                "model_id": settings.watsonx_embedding_model,
                "project_id": settings.watsonx_project_id,
                "inputs": [chunk.text],
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                embedding = data["results"][0]["embedding"]
                results.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "text": chunk.text,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                        "source_file": chunk.source_file,
                        "embedding": embedding,
                    }
                )
            except Exception as exc:
                logger.error("watsonx embedding failed for chunk %s: %s", chunk.chunk_id, exc)

    return results
