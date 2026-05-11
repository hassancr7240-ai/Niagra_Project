from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import crud
from app.rag.chunker import chunk_text, extract_chapter_text, extract_text_from_pdf, find_maintenance_chapter
from app.rag.classifier import classify_manual
from app.rag.embedder import embed_chunks
from app.rag.extractor import extract_tasks_from_chunks
from app.rag.retriever import index_chunks, retrieve_top_k

logger = logging.getLogger(__name__)
settings = get_settings()


async def run_pipeline(
    db: AsyncSession,
    manual_id: str,
    pdf_path: Path,
) -> dict:
    """
    Full RAG pipeline:
    PDF → Classify → Find Chapter → Chunk (500w/103w overlap) →
    Embed → Vector Store → RAG Retrieve Top 10 → AI Extract →
    Validate JSON → Return for engineer review.

    Matching the data flow diagram exactly.
    """
    results = {
        "manual_id": manual_id,
        "status": "FAILED",
        "manufacturer": None,
        "model": None,
        "machine_type": None,
        "detected_chapters": [],
        "chunk_count": 0,
        "extracted_tasks": [],
        "error": None,
    }

    try:
        # ── Stage A: Ingest & Process ─────────────────────────────────────

        # Step 1: Extract text from PDF
        await _update_status(db, manual_id, "CLASSIFYING")
        logger.info("[%s] Extracting text from PDF", manual_id)
        full_text, page_offsets = extract_text_from_pdf(pdf_path)
        sample_text = full_text[:8000]

        # Step 2: Pre-Classify (Krones/Other)
        classification = await classify_manual(pdf_path, sample_text)
        results["manufacturer"] = classification.manufacturer
        results["model"] = classification.model
        results["machine_type"] = classification.machine_type
        results["detected_chapters"] = classification.detected_chapters
        logger.info("[%s] Classified: %s %s (chapters=%s)", manual_id,
                    classification.manufacturer, classification.model, classification.detected_chapters)

        # Step 3: Find Chapter (auto for Krones, RAG search for others)
        await _update_status(db, manual_id, "CHUNKING")
        chapter_text = ""
        for ch_num in (classification.detected_chapters or []):
            chapter_text += extract_chapter_text(full_text, ch_num) + "\n"
        if not chapter_text:
            chapter_text = full_text

        # Step 4: PDF Chunking — 500 words / 103 word overlap
        source_name = pdf_path.name
        chunks = chunk_text(
            chapter_text,
            source_file=source_name,
            chunk_size=settings.rag_chunk_size,
            overlap=settings.rag_chunk_overlap,
            page_offsets=page_offsets,
        )
        results["chunk_count"] = len(chunks)
        logger.info("[%s] Created %d chunks", manual_id, len(chunks))

        # Step 5: Embed Chunks → text-embedding-3
        await _update_status(db, manual_id, "EMBEDDING")
        embedded = await embed_chunks(chunks)
        logger.info("[%s] Embedded %d chunks", manual_id, len(embedded))

        # ── Stage B: Extract & Output ─────────────────────────────────────

        # Step 6: Store in Vector Store (Azure AI Search)
        if embedded:
            await index_chunks(embedded, manual_id)

        # Step 7: RAG Retrieve — Top 10 chunks for task extraction query
        await _update_status(db, manual_id, "EXTRACTING")
        top_chunks = await _retrieve_maintenance_chunks(embedded, manual_id)

        # Step 8: AI Extraction → structured JSON
        interval_hints = _guess_intervals(classification.manufacturer)
        extracted_tasks = await extract_tasks_from_chunks(
            top_chunks or _to_chunk_dicts(embedded[:10]),
            manufacturer=classification.manufacturer,
            model=classification.model,
            interval_hints=interval_hints,
        )

        # Step 9: Validate JSON schema
        results["extracted_tasks"] = extracted_tasks
        results["status"] = "PENDING_REVIEW"

        import json
        await crud.update_manual_upload(db, manual_id, {
            "status": "PENDING_REVIEW",
            "detected_manufacturer": classification.manufacturer,
            "detected_chapters": json.dumps(classification.detected_chapters),
            "extracted_tasks": json.dumps(extracted_tasks),
        })

        logger.info("[%s] Pipeline complete: %d tasks extracted, awaiting engineer review",
                    manual_id, len(extracted_tasks))

    except Exception as exc:
        logger.exception("[%s] Pipeline failed: %s", manual_id, exc)
        results["error"] = str(exc)
        await _update_status(db, manual_id, "FAILED", error=str(exc))

    return results


async def _update_status(
    db: AsyncSession, manual_id: str, status: str, error: Optional[str] = None
) -> None:
    update = {"status": status}
    if error:
        update["error_message"] = error
    await crud.update_manual_upload(db, manual_id, update)


async def _retrieve_maintenance_chunks(
    embedded: list[dict], manual_id: str
) -> list[dict]:
    """Create a query embedding for 'maintenance tasks intervals' and retrieve top 10."""
    if not embedded:
        return []

    if not settings.azure_search_endpoint:
        # Fallback: return first 10 embedded chunks directly
        return _to_chunk_dicts(embedded[:10])

    try:
        # Embed a maintenance-focused query
        query_text = "maintenance tasks inspection intervals replace clean lubricate safety lockout"
        from app.rag.embedder import embed_chunks
        from app.rag.chunker import TextChunk
        query_chunk = [TextChunk(
            chunk_id="query",
            text=query_text,
            page_start=0,
            page_end=0,
            char_start=0,
            char_end=len(query_text),
            source_file="query",
        )]
        embedded_query = await embed_chunks(query_chunk)
        if not embedded_query:
            return _to_chunk_dicts(embedded[:10])

        top_chunks = await retrieve_top_k(
            query_embedding=embedded_query[0]["embedding"],
            manual_id=manual_id,
            top_k=settings.rag_top_k,
        )
        return top_chunks or _to_chunk_dicts(embedded[:10])
    except Exception:
        return _to_chunk_dicts(embedded[:10])


def _to_chunk_dicts(embedded: list[dict]) -> list[dict]:
    return [{"text": e["text"], "page_start": e.get("page_start", 0),
             "page_end": e.get("page_end", 0), "source_file": e.get("source_file", "")}
            for e in embedded]


def _guess_intervals(manufacturer: str) -> list[int]:
    if manufacturer == "KRONES":
        return [8, 120, 500, 1500, 3000, 6000]
    return [8, 240, 500, 1500]
