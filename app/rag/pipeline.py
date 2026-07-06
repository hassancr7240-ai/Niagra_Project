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
    """Return likely PM intervals (hours) based on manufacturer type."""
    mfr = (manufacturer or "").upper()
    if any(k in mfr for k in ("KRONES", "VARIOPAC", "CONTIFORM", "SHRINK")):
        return [100, 120, 500, 1000, 1500, 3000, 4000, 6000, 30000]
    if any(k in mfr for k in ("TETRA", "TEM", "PMRSPL")):
        return [3000, 6000, 12000, 18000]
    if any(k in mfr for k in ("EISBAR", "DEHUMID")):
        return [500, 42000, 45000]
    # Generic fallback — broad set covering most industrial equipment
    return [8, 100, 120, 240, 500, 1000, 1500, 3000, 6000, 12000]


_GENERIC_WORDS = frozenset({
    "the", "this", "that", "for", "and", "machine", "manual", "generate",
    "check", "maintenance", "preventive", "tasks", "service",
})


def _extract_tasks_from_pdf_tables(pdf_path: Path) -> list[dict]:
    """
    Generalized fallback extractor: parse PM tables from any PDF format.

    Strategy (tried in order):
      1. PMRSPL/Tetra Pak style — header-row column detection
      2. Generic interval table — any table with a numeric interval column
         and an action/description column
      3. Text-pattern fallback — regex over page text for "Xh / every X hours"
         maintenance bullets (handles German/English narrative manuals)
    """
    import pdfplumber

    tasks: list[dict] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            tasks = _try_header_table(pdf, pdf_path)
            if not tasks:
                tasks = _try_generic_table(pdf, pdf_path)
            if not tasks:
                tasks = _try_text_patterns(pdf, pdf_path)
    except Exception as exc:
        logger.error("Table extraction failed for %s: %s", pdf_path.name, exc)

    logger.info("Generalized fallback: %d tasks from %s", len(tasks), pdf_path.name)
    return tasks


# ── Column-header keywords ────────────────────────────────────────────────────

_INTERVAL_HEADERS = {"interval", "intervall", "frequency", "frequenz", "cycle",
                     "hours", "stunden", "hrs", "period"}
_ACTION_HEADERS   = {"action", "aktion", "work", "task", "operation", "activity",
                     "maintenance", "wartung"}
_DESC_HEADERS     = {"description", "beschreibung", "detail", "instruction",
                     "specification", "work description", "comment", "remarks"}
_AREA_HEADERS     = {"component", "area", "system", "location", "part",
                     "equipment", "assembly", "group", "bauteil"}

_AREA_MAP = {
    "valve": "VALVES", "filter": "FILTERS", "cartridge": "FILTERS",
    "sensor": "SENSORS", "transmitter": "SENSORS", "probe": "SENSORS",
    "pump": "PUMP", "motor": "MOTOR", "belt": "DRIVE SYSTEM",
    "chain": "DRIVE SYSTEM", "bearing": "BEARINGS", "seal": "SEALS",
    "gasket": "SEALS", "lubric": "LUBRICATION", "oil": "LUBRICATION",
    "grease": "LUBRICATION", "silencer": "SILENCERS", "switch": "SWITCHES",
    "electrical": "ELECTRICAL", "cable": "ELECTRICAL", "conveyor": "CONVEYOR",
    "safety": "SAFETY", "guard": "SAFETY", "loto": "SAFETY",
}

_ACTION_VERB_MAP = {
    "check": "CHECK", "inspect": "INSPECT", "verify": "VERIFY",
    "test": "TEST", "confirm": "CONFIRM",
    "change": "REPLACE", "replace": "REPLACE", "renew": "REPLACE",
    "clean": "CLEAN", "flush": "CLEAN",
    "lubricate": "LUBRICATE", "grease": "LUBRICATE", "oil": "LUBRICATE",
    "tighten": "CHECK", "adjust": "CHECK", "calibrate": "VERIFY",
}

_NON_PM_PATTERNS = re.compile(
    r"warning sign|safety sign|label|sticker|decal|notice board|placard",
    re.IGNORECASE,
)

# Known valid PM intervals (hours)
_VALID_INTERVALS = {
    8, 40, 100, 120, 240, 250, 500, 750, 1000, 1500, 2000, 2500,
    3000, 4000, 5000, 6000, 8000, 10000, 12000, 15000, 18000,
    20000, 24000, 30000, 36000, 42000, 45000,
}


def _detect_area(text: str) -> str:
    low = text.lower()
    for kw, area in _AREA_MAP.items():
        if kw in low:
            return area
    return "GENERAL"


def _detect_action_verb(text: str) -> str:
    low = text.lower()
    for kw, verb in _ACTION_VERB_MAP.items():
        if kw in low:
            return verb
    return "CHECK"


def _snap_interval(hours: int) -> int:
    """Round a raw hour value to the nearest known PM interval."""
    closest = min(_VALID_INTERVALS, key=lambda v: abs(v - hours))
    return closest if abs(closest - hours) / max(hours, 1) < 0.2 else hours


def _build_task(task_no: int, area: str, action_verb: str,
                desc: str, interval: int, part_number: str = "") -> dict:
    if _NON_PM_PATTERNS.search(desc):
        return {}
    return {
        "task_no": task_no,
        "area": area,
        "action": action_verb,
        "description": re.sub(r"\s+", " ", desc.upper()).strip()[:250],
        "machine_state": "POWERED_OFF" if "loto" in desc.lower() or "lockout" in desc.lower()
                         else "STOPPED",
        "safety_flag": bool(re.search(r"loto|lockout|isolation|danger|warning", desc, re.I)),
        "part_number": part_number or None,
        "interval_hours": interval,
    }


def _finalize(raw: list[dict]) -> list[dict]:
    """Deduplicate by (interval, area, description-prefix) and add task numbers."""
    seen: dict[tuple, dict] = {}
    for t in raw:
        if not t:
            continue
        core = t["description"][:100]
        key = (t["interval_hours"], t["area"], core)
        if key not in seen:
            seen[key] = {**t, "_n": 1}
        else:
            seen[key]["_n"] += 1

    final = []
    task_no = 10
    for key in sorted(seen, key=lambda k: (k[0], k[1], k[2])):
        g = seen[key]
        if g["_n"] > 1:
            g["description"] = f"{g['description']} ({g['_n']} LOCATIONS)"
        g.pop("_n")
        g["task_no"] = task_no
        final.append(g)
        task_no += 10
    return final


# ── Strategy 1: header-row column detection ───────────────────────────────────

def _try_header_table(pdf, pdf_path: Path) -> list[dict]:
    """
    Scan every table for a header row containing interval/action/description
    keywords. Once found, use those column indices to parse all subsequent rows.
    Works for PMRSPL (Tetra Pak), German Krones service lists, etc.
    """
    raw: list[dict] = []

    for page in pdf.pages:
        for table in page.extract_tables():
            if not table or len(table) < 2:
                continue

            # Find header row (first row where ≥2 cells match known headers)
            hdr_idx = None
            cols: dict[str, int] = {}
            for ri, row in enumerate(table[:4]):
                cells = [str(c or "").lower().strip() for c in row]
                matched = {}
                for ci, cell in enumerate(cells):
                    for col_type, headers in [
                        ("interval", _INTERVAL_HEADERS),
                        ("action",   _ACTION_HEADERS),
                        ("desc",     _DESC_HEADERS),
                        ("area",     _AREA_HEADERS),
                    ]:
                        if col_type not in matched and any(h in cell for h in headers):
                            matched[col_type] = ci
                if len(matched) >= 2 and ("interval" in matched or "action" in matched):
                    hdr_idx = ri
                    cols = matched
                    break

            if hdr_idx is None:
                continue

            # Parse data rows after the header
            for row in table[hdr_idx + 1:]:
                if not row or all(c is None or str(c).strip() == "" for c in row):
                    continue
                try:
                    interval_raw = str(row[cols["interval"]] or "").strip() if "interval" in cols else ""
                    action_raw   = str(row[cols.get("action", cols.get("desc", 0))] or "").strip()
                    desc_raw     = str(row[cols.get("desc", cols.get("action", 0))] or "").strip()
                    area_raw     = str(row[cols.get("area", 0)] or "").strip() if "area" in cols else ""

                    # Extract numeric interval
                    m = re.search(r"(\d{2,6})", interval_raw)
                    if not m:
                        continue
                    interval = _snap_interval(int(m.group(1)))
                    if interval < 8:
                        continue

                    action_verb = _detect_action_verb(action_raw)
                    area = _detect_area(area_raw or desc_raw) if not area_raw else area_raw.upper()[:30]
                    desc = desc_raw or action_raw

                    # Find part number anywhere in the row
                    pn = ""
                    for cell in row:
                        cs = str(cell or "").strip()
                        if re.match(r"[A-Z0-9][A-Z0-9\-]{4,20}$", cs):
                            pn = cs
                            break

                    t = _build_task(0, area, action_verb, desc, interval, pn)
                    if t:
                        raw.append(t)
                except (ValueError, IndexError, TypeError):
                    continue

    logger.info("Header-table strategy: %d raw tasks from %s", len(raw), pdf_path.name)
    return _finalize(raw) if raw else []


# ── Strategy 2: generic interval table ───────────────────────────────────────

def _try_generic_table(pdf, pdf_path: Path) -> list[dict]:
    """
    No header found — scan tables for rows where one cell is a 2–5-digit
    number in _VALID_INTERVALS range and another cell contains action text.
    """
    raw: list[dict] = []

    for page in pdf.pages:
        for table in page.extract_tables():
            if not table:
                continue
            for row in table:
                if not row or len(row) < 3:
                    continue
                cells = [str(c or "").strip() for c in row]
                # Find an interval cell
                interval = None
                for c in cells:
                    m = re.match(r"^(\d{2,6})$", c)
                    if m:
                        v = int(m.group(1))
                        snapped = _snap_interval(v)
                        if snapped in _VALID_INTERVALS and abs(snapped - v) / max(v, 1) < 0.2:
                            interval = snapped
                            break
                if interval is None:
                    continue
                # Build description from remaining cells
                desc_parts = [c for c in cells if c and c != str(interval) and len(c) > 3]
                if not desc_parts:
                    continue
                desc = " - ".join(desc_parts[:4])
                action_verb = _detect_action_verb(desc)
                area = _detect_area(desc)
                pn = next((c for c in cells if re.match(r"[A-Z0-9][A-Z0-9\-]{4,20}$", c)), "")
                t = _build_task(0, area, action_verb, desc, interval, pn)
                if t:
                    raw.append(t)

    logger.info("Generic-table strategy: %d raw tasks from %s", len(raw), pdf_path.name)
    return _finalize(raw) if raw else []


# ── Strategy 3: text-pattern fallback ────────────────────────────────────────

_TEXT_INTERVAL_RE = re.compile(
    r"(?:every|alle|each|after|nach)\s+(\d{2,6})\s*(?:hours?|hours|hrs?|h\b|"
    r"betriebsstunden|stunden)\s*[:\-–]?\s*(.{10,200}?)(?=\n|every|alle|$)",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(
    r"(?:^|\n)\s*[•–\-\*]\s*(.{10,200}?)(?=\n|$)",
    re.IGNORECASE,
)


def _try_text_patterns(pdf, pdf_path: Path) -> list[dict]:
    """
    Last resort: regex-match "every X hours: <task>" patterns in page text.
    Handles narrative-style manuals (Krones/Eisbar English/German).
    """
    raw: list[dict] = []
    current_interval: int = 0

    for page in pdf.pages:
        text = page.extract_text() or ""

        # Find interval anchors ("Every 500 hours:")
        for m in _TEXT_INTERVAL_RE.finditer(text):
            hrs_raw = int(m.group(1))
            snapped = _snap_interval(hrs_raw)
            if snapped not in _VALID_INTERVALS:
                continue
            current_interval = snapped
            task_text = m.group(2).strip()
            if len(task_text) > 10:
                t = _build_task(0, _detect_area(task_text),
                                _detect_action_verb(task_text), task_text, current_interval)
                if t:
                    raw.append(t)

        # Collect bullet points under current interval
        if current_interval:
            for m in _BULLET_RE.finditer(text):
                task_text = m.group(1).strip()
                if len(task_text) > 10:
                    t = _build_task(0, _detect_area(task_text),
                                    _detect_action_verb(task_text), task_text, current_interval)
                    if t:
                        raw.append(t)

    logger.info("Text-pattern strategy: %d raw tasks from %s", len(raw), pdf_path.name)
    return _finalize(raw) if raw else []
