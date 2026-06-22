from __future__ import annotations

import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Path to the same SQLite file the rest of the app uses
_DB_PATH = Path(__file__).parent.parent.parent / "data" / "pm_automation.db"


# ── Local vector store helpers ────────────────────────────────────────────────

def _ensure_chunks_table() -> None:
    """Create the manual_chunks table if it doesn't exist yet."""
    conn = sqlite3.connect(_DB_PATH, timeout=60, isolation_level=None)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_chunks (
            chunk_id    TEXT PRIMARY KEY,
            manual_id   TEXT NOT NULL,
            text        TEXT NOT NULL,
            page_start  INTEGER DEFAULT 0,
            page_end    INTEGER DEFAULT 0,
            source_file TEXT    DEFAULT '',
            embedding   TEXT    NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_manual ON manual_chunks(manual_id)"
    )
    conn.commit()
    conn.close()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _local_index(chunks_with_embeddings: list[dict], manual_id: str) -> int:
    """Save embedded chunks to SQLite — replaces any previous upload for this manual."""
    _ensure_chunks_table()
    conn = sqlite3.connect(_DB_PATH, timeout=60, isolation_level=None)
    try:
        conn.execute("DELETE FROM manual_chunks WHERE manual_id = ?", (manual_id,))
        rows = [
            (
                c["chunk_id"],
                manual_id,
                c["text"],
                c.get("page_start", 0),
                c.get("page_end", 0),
                c.get("source_file", ""),
                json.dumps(c["embedding"]),
            )
            for c in chunks_with_embeddings
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO manual_chunks "
            "(chunk_id, manual_id, text, page_start, page_end, source_file, embedding) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        logger.info("Local vector store: saved %d chunks for manual_id=%s", len(rows), manual_id)
        return len(rows)
    finally:
        conn.close()


def _local_retrieve(
    query_embedding: list[float],
    manual_id: Optional[str],
    top_k: int,
) -> list[dict]:
    """Cosine-similarity search over locally stored chunks."""
    _ensure_chunks_table()
    conn = sqlite3.connect(_DB_PATH, timeout=60, isolation_level=None)
    try:
        if manual_id:
            rows = conn.execute(
                "SELECT text, page_start, page_end, source_file, embedding "
                "FROM manual_chunks WHERE manual_id = ?",
                (manual_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text, page_start, page_end, source_file, embedding "
                "FROM manual_chunks"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    scored = []
    for text, page_start, page_end, source_file, emb_json in rows:
        try:
            emb = json.loads(emb_json)
            score = _cosine_similarity(query_embedding, emb)
            scored.append(
                {
                    "text": text,
                    "page_start": page_start,
                    "page_end": page_end,
                    "source_file": source_file,
                    "score": score,
                }
            )
        except Exception:
            continue

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ── Public API ────────────────────────────────────────────────────────────────

async def index_chunks(chunks_with_embeddings: list[dict], manual_id: str) -> int:
    """
    Store embedded chunks in the vector store.
    Uses Azure AI Search when configured, falls back to local SQLite otherwise.
    """
    if not settings.azure_search_endpoint:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _local_index, chunks_with_embeddings, manual_id)

    try:
        from azure.search.documents.aio import SearchClient
        from azure.core.credentials import AzureKeyCredential

        client = SearchClient(
            endpoint=settings.azure_search_endpoint,
            index_name=settings.azure_search_index_name,
            credential=AzureKeyCredential(settings.azure_search_api_key or ""),
        )
        documents = [
            {
                "id": c["chunk_id"].replace("/", "_").replace(".", "_"),
                "manual_id": manual_id,
                "text": c["text"],
                "page_start": c["page_start"],
                "page_end": c["page_end"],
                "source_file": c["source_file"],
                "content_vector": c["embedding"],
            }
            for c in chunks_with_embeddings
        ]
        async with client:
            result = await client.upload_documents(documents=documents)
        succeeded = sum(1 for r in result if r.succeeded)
        logger.info("Azure Search: indexed %d/%d chunks for manual_id=%s",
                    succeeded, len(documents), manual_id)
        return succeeded

    except Exception as exc:
        logger.error("Azure Search indexing failed, falling back to local: %s", exc)
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, _local_index, chunks_with_embeddings, manual_id)


async def retrieve_top_k(
    query_embedding: list[float],
    manual_id: Optional[str] = None,
    top_k: int = 10,
) -> list[dict]:
    """
    Find the top-k most semantically similar chunks to the query.
    Uses Azure AI Search when configured, falls back to local SQLite cosine search.
    """
    if not settings.azure_search_endpoint:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _local_retrieve, query_embedding, manual_id, top_k)

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
        logger.error("Azure Search retrieval failed, falling back to local: %s", exc)
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, _local_retrieve, query_embedding, manual_id, top_k)
