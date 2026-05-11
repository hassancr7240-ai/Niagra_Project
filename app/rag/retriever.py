from __future__ import annotations

import logging
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def index_chunks(chunks_with_embeddings: list[dict], manual_id: str) -> int:
    """Upload embedded chunks to Azure AI Search index."""
    if not settings.azure_search_endpoint:
        logger.warning("Azure AI Search not configured — skipping index upload")
        return 0

    try:
        from azure.search.documents.aio import SearchClient
        from azure.core.credentials import AzureKeyCredential

        client = SearchClient(
            endpoint=settings.azure_search_endpoint,
            index_name=settings.azure_search_index_name,
            credential=AzureKeyCredential(settings.azure_search_api_key or ""),
        )

        documents = []
        for c in chunks_with_embeddings:
            documents.append(
                {
                    "id": c["chunk_id"].replace("/", "_").replace(".", "_"),
                    "manual_id": manual_id,
                    "text": c["text"],
                    "page_start": c["page_start"],
                    "page_end": c["page_end"],
                    "source_file": c["source_file"],
                    "content_vector": c["embedding"],
                }
            )

        async with client:
            result = await client.upload_documents(documents=documents)

        succeeded = sum(1 for r in result if r.succeeded)
        logger.info("Indexed %d/%d chunks for manual_id=%s", succeeded, len(documents), manual_id)
        return succeeded

    except Exception as exc:
        logger.error("Azure Search indexing failed: %s", exc)
        return 0


async def retrieve_top_k(
    query_embedding: list[float],
    manual_id: Optional[str] = None,
    top_k: int = 10,
) -> list[dict]:
    """Retrieve top-k semantically similar chunks from Azure AI Search."""
    if not settings.azure_search_endpoint:
        logger.warning("Azure AI Search not configured — returning empty results")
        return []

    try:
        from azure.search.documents.aio import SearchClient
        from azure.search.documents.models import VectorizedQuery
        from azure.core.credentials import AzureKeyCredential

        client = SearchClient(
            endpoint=settings.azure_search_endpoint,
            index_name=settings.azure_search_index_name,
            credential=AzureKeyCredential(settings.azure_search_api_key or ""),
        )

        vector_query = VectorizedQuery(
            vector=query_embedding,
            k_nearest_neighbors=top_k,
            fields="content_vector",
        )
        filter_str = f"manual_id eq '{manual_id}'" if manual_id else None

        async with client:
            results = await client.search(
                search_text=None,
                vector_queries=[vector_query],
                filter=filter_str,
                select=["text", "page_start", "page_end", "source_file", "manual_id"],
                top=top_k,
            )

            chunks = []
            async for r in results:
                chunks.append(
                    {
                        "text": r.get("text", ""),
                        "page_start": r.get("page_start", 0),
                        "page_end": r.get("page_end", 0),
                        "source_file": r.get("source_file", ""),
                        "score": r.get("@search.score", 0.0),
                    }
                )
        return chunks

    except Exception as exc:
        logger.error("Azure Search retrieval failed: %s", exc)
        return []
