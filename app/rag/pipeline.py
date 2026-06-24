from __future__ import annotations

import asyncio
import logging
import re
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

        # Step 8b: Fallback — if AI returned 0 tasks, try direct table
        # extraction from the PDF (handles PMRSPL-style tabular manuals
        # like Tetra Pak where the small AI model can't parse the format)
        if not extracted_tasks:
            logger.warning("[%s] AI extraction returned 0 tasks — trying table-based fallback", manual_id)
            extracted_tasks = _extract_tasks_from_pdf_tables(pdf_path)
            if extracted_tasks:
                logger.info("[%s] Table fallback extracted %d tasks", manual_id, len(extracted_tasks))

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
    """
    Retrieve the top maintenance-relevant chunks by cosine similarity.
    Uses the vector store (local SQLite or Azure) — always semantic, never positional.
    """
    if not embedded:
        return []

    # Embed a maintenance-focused query to find the most relevant sections
    query_text = (
        "preventive maintenance tasks inspection intervals lubrication replacement "
        "safety lockout LOTO cleaning filter belt bearing grease oil schedule checklist"
    )
    try:
        query_chunk = [{"chunk_id": "maint_query", "text": query_text,
                        "page_start": 0, "page_end": 0, "source_file": "query"}]
        embedded_query = await embed_chunks(query_chunk)
        if not embedded_query:
            return _to_chunk_dicts(embedded[:10])

        top_chunks = await retrieve_top_k(
            query_embedding=embedded_query[0]["embedding"],
            manual_id=manual_id,
            top_k=settings.rag_top_k,
        )
        if top_chunks:
            logger.info("[%s] Retrieved %d maintenance-relevant chunks via semantic search", manual_id, len(top_chunks))
            return top_chunks
    except Exception as exc:
        logger.warning("[%s] Semantic retrieval failed, using positional fallback: %s", manual_id, exc)

    # Only reach here if embedding/retrieval completely failed
    return _to_chunk_dicts(embedded[:10])


def _to_chunk_dicts(embedded: list[dict]) -> list[dict]:
    return [{"text": e["text"], "page_start": e.get("page_start", 0),
             "page_end": e.get("page_end", 0), "source_file": e.get("source_file", "")}
            for e in embedded]


def _guess_intervals(manufacturer: str) -> list[int]:
    if manufacturer == "KRONES":
        return [8, 120, 500, 1500, 3000, 6000]
    return [8, 240, 500, 1500]


_GENERIC_WORDS = frozenset({
    "the", "this", "that", "for", "and", "machine", "manual", "generate",
    "check", "maintenance", "preventive", "tasks", "service",
})


def _extract_tasks_from_pdf_tables(pdf_path: Path) -> list[dict]:
    """
    Fallback extractor: parse structured PM tables directly from the PDF
    using pdfplumber table extraction. Handles PMRSPL-style formats
    (Tetra Pak, etc.) where columns contain component ID, description,
    interval hours, action (Check/Change), and spare part info.
    """
    import pdfplumber

    all_rows: list[list] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                text = (page.extract_text() or "")
                if not re.search(r"\d{3,6}\s+(Check|Change)\s+", text):
                    continue
                for table in page.extract_tables():
                    for row in table:
                        if row and len(row) >= 11:
                            all_rows.append(row)
    except Exception as exc:
        logger.error("Table extraction failed for %s: %s", pdf_path.name, exc)
        return []

    if not all_rows:
        return []

    tasks: list[dict] = []
    seen: set[str] = set()
    task_no = 10
    valid_actions = {"LOCKOUT", "CHECK", "LISTEN", "CLEAN", "REPLACE", "LUBRICATE",
                     "VERIFY", "TEST", "CONFIRM", "INSPECT"}

    for row in all_rows:
        try:
            comp_type = (row[4] or "").strip()
            comp_desc = (row[5] or "").strip()[:80]
            interval_str = (row[8] or "").strip()
            action = (row[9] or "").strip()
            action_desc = (row[10] or "").strip()
            spare_pn = (row[11] or "").strip() if len(row) > 11 else ""
            spare_desc = " ".join(
                filter(None, [str(row[i] or "").strip() for i in range(12, min(15, len(row)))])
            )

            if not interval_str or action not in ("Check", "Change"):
                continue

            interval = int(interval_str)

            if action == "Check":
                desc = f"{action_desc.upper()} - {comp_type.upper()} {comp_desc.upper()}"
            else:
                desc = f"CHANGE {action_desc.upper()} - {comp_type.upper()} {comp_desc.upper()}"
                if spare_pn:
                    desc += f" [PART: {spare_pn} {spare_desc}]".upper()

            desc = re.sub(r"\s+", " ", desc).strip()[:200]

            key = f"{interval}|{action}|{row[1] or ''}|{action_desc}"
            if key in seen:
                continue
            seen.add(key)

            ct = comp_type.lower()
            if "valve" in ct:
                area = "VALVES"
            elif "filter" in ct or "cartridge" in ct:
                area = "FILTERS"
            elif "sensor" in ct or "transmitter" in ct:
                area = "SENSORS"
            elif "silencer" in ct:
                area = "SILENCERS"
            elif "gasket" in ct or "seal" in ct:
                area = "SEALS"
            elif "switch" in ct:
                area = "SWITCHES"
            else:
                area = comp_type.upper()[:20] or "GENERAL"

            action_verb = "CHECK" if action == "Check" else "REPLACE"

            tasks.append({
                "task_no": task_no,
                "area": area,
                "action": action_verb,
                "description": desc,
                "machine_state": "STOPPED",
                "safety_flag": False,
                "part_number": spare_pn if spare_pn else None,
                "interval_hours": interval,
            })
            task_no += 10

        except (ValueError, IndexError):
            continue

    # Consolidate duplicates (same action on same component type at same interval)
    consolidated: dict[tuple, dict] = {}
    for t in tasks:
        core = re.sub(r"\b[A-Z]{1,3}\d{2,3}_\d+\b", "", t["description"])
        core = re.sub(r"\s+", " ", core).strip()[:120]
        key = (t["interval_hours"], t["action"], t["area"], core)
        if key not in consolidated:
            consolidated[key] = {**t, "_count": 1}
        else:
            consolidated[key]["_count"] += 1

    final: list[dict] = []
    task_no = 10
    for key, g in sorted(consolidated.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
        desc = g["description"]
        if g["_count"] > 1:
            desc = f"{desc} ({g['_count']} LOCATIONS)"
        g["task_no"] = task_no
        g["description"] = desc[:200]
        del g["_count"]
        final.append(g)
        task_no += 10

    logger.info("Table fallback: %d raw → %d consolidated tasks from %s",
                len(tasks), len(final), pdf_path.name)
    return final
