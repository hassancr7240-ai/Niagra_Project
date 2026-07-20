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
from app.rag.chunker import TextChunk, smart_chunk_pdf, extract_text_from_pdf
from app.rag.classifier import classify_manual
from app.rag.embedder import embed_chunks
from app.rag.extractor import extract_tasks_from_chunks
from app.rag.retriever import index_chunks, retrieve_top_k

# ── 8-Pass retrieval config ────────────────────────────────────────────────────
# Each pass targets a specific content type. For local dev, filters by content_type.
# For production (Azure AI Search), these become separate hybrid search queries.
_RETRIEVAL_PASSES: list[tuple[str, str, int]] = [
    # (pass_name, content_type_filter, max_chunks_per_pass)
    ("toc_schedule",  "toc",        4),
    ("warnings",      "warning",    5),
    ("loto",          "loto",       4),
    ("interval",      "table",     15),   # interval tasks — table_row + checkbox get priority
    ("ppe_tools",     "ppe",        4),
    ("startup",       "startup",    4),
    ("parts",         "parts_list", 5),
    ("coverage",      "procedure", 10),   # final sweep — best remaining procedure chunks
]

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

        await _update_status(db, manual_id, "CLASSIFYING")
        logger.info("[%s] Starting text extraction + chunking (both in thread pool)", manual_id)

        # Both run in thread pool simultaneously — neither blocks the event loop
        # extract_text_from_pdf is capped at 60 pages → finishes in ~10s
        source_name = pdf_path.name
        text_task = asyncio.ensure_future(
            asyncio.to_thread(extract_text_from_pdf, pdf_path)
        )
        chunk_task = asyncio.ensure_future(
            asyncio.to_thread(
                smart_chunk_pdf, pdf_path, source_name,
                settings.rag_max_section_words, settings.rag_min_section_words,
                manual_id, "",  # manual_id passed so every chunk carries it; version unknown at chunk time
            )
        )
        # Wait for text extraction (fast — capped at 60 pages), then classify
        full_text, _offsets = await text_task
        sample_text = full_text[:15000]

        try:
            classification = await asyncio.wait_for(
                classify_manual(pdf_path, sample_text), timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("[%s] Classification timed out — using keyword fallback", manual_id)
            from app.rag.classifier import _keyword_classify
            classification = _keyword_classify(sample_text)

        await _update_status(db, manual_id, "CHUNKING")
        chunks = await chunk_task
        results["manufacturer"] = classification.manufacturer
        results["model"] = classification.model
        results["machine_type"] = classification.machine_type
        results["detected_chapters"] = classification.detected_chapters
        results["chunk_count"] = len(chunks)
        type_summary = ', '.join(
            f'{t}={sum(1 for c in chunks if c.chunk_type == t)}'
            for t in dict.fromkeys(c.chunk_type for c in chunks)
        )
        logger.info("[%s] Classified: %s %s | %d chunks (%s)", manual_id,
                    classification.manufacturer, classification.model, len(chunks), type_summary)

        # ── Stage B: Select/Embed → Retrieve → Extract ───────────────────

        await _update_status(db, manual_id, "EMBEDDING")

        # Merge manufacturer defaults with intervals detected from document structure
        chunk_intervals = list({c.interval_hint for c in chunks if c.interval_hint})
        interval_hints = list(set(chunk_intervals) | set(_guess_intervals(classification.manufacturer)))

        if settings.ai_provider != "watsonx":
            # Local dev fast path: skip embedding — use 8-pass content_type filtering instead
            top_chunks = _retrieve_8_pass_local(chunks, interval_hints)
            if not top_chunks:
                top_chunks = _select_top_chunks_by_type(chunks, settings.rag_top_k)
            embedded = []
            logger.info("[%s] Local dev 8-pass: selected %d chunks across %d passes",
                        manual_id, len(top_chunks), len(_RETRIEVAL_PASSES))
        else:
            # Production: full embed + index + Azure AI Search hybrid retrieval
            priority = [c for c in chunks if c.chunk_type in ("table_row", "checkbox")]
            others   = [c for c in chunks if c.chunk_type not in ("table_row", "checkbox")]
            embed_subset = (priority + others)[:200]
            logger.info("[%s] Embedding %d/%d chunks", manual_id, len(embed_subset), len(chunks))
            embedded = await embed_chunks(embed_subset)
            logger.info("[%s] Embedded %d chunks", manual_id, len(embedded))
            if embedded:
                await index_chunks(embedded, manual_id)
            top_chunks = await _retrieve_maintenance_chunks(embedded, manual_id)

        await _update_status(db, manual_id, "EXTRACTING")

        # AI Extraction → structured JSON tasks
        extracted_tasks = await extract_tasks_from_chunks(
            top_chunks or _to_chunk_dicts(embedded[:10]),
            manufacturer=classification.manufacturer,
            model=classification.model,
            interval_hints=interval_hints,
        )

        # Fallback — if AI returned 0 tasks, try direct table extraction
        # (handles PMRSPL-style tabular manuals like Tetra Pak)
        if not extracted_tasks:
            logger.warning("[%s] AI extraction returned 0 tasks — trying table-based fallback", manual_id)
            extracted_tasks = _extract_tasks_from_pdf_tables(pdf_path)
            if extracted_tasks:
                logger.info("[%s] Table fallback extracted %d tasks", manual_id, len(extracted_tasks))

        # Save citations — one record per top chunk so UI can show page/section links
        citation_records = [
            {
                "manual_id": manual_id,
                "chunk_id": c.get("chunk_id", ""),
                "page_start": c.get("page_start", 0),
                "page_end": c.get("page_end", 0),
                "section": c.get("section", ""),
                "content_type": c.get("content_type", "procedure"),
                "text_excerpt": c.get("text", "")[:500],
                "manual_version": c.get("manual_version", ""),
            }
            for c in top_chunks
            if c.get("page_start", 0) > 0  # skip chunks with no page info
        ]
        if citation_records:
            saved = await crud.save_citations(db, citation_records)
            await db.commit()
            logger.info("[%s] Saved %d citations", manual_id, saved)

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
    Skips an extra embed call by picking a pre-embedded chunk as the query proxy,
    falling back to a separate embed only when no table/checkbox chunks exist.
    """
    if not embedded:
        return []

    # Fast path: use a table_row or checkbox chunk's own embedding as the query proxy
    # (these chunks already represent maintenance content precisely)
    proxy = next(
        (e for e in embedded if e.get("chunk_type") in ("table_row", "checkbox")),
        None,
    )

    if proxy:
        try:
            top_chunks = await retrieve_top_k(
                query_embedding=proxy["embedding"],
                manual_id=manual_id,
                top_k=settings.rag_top_k,
            )
            if top_chunks:
                logger.info("[%s] Retrieved %d chunks via proxy embedding (no extra embed call)",
                            manual_id, len(top_chunks))
                return top_chunks
        except Exception as exc:
            logger.warning("[%s] Proxy retrieval failed: %s", manual_id, exc)

    # Slow path: embed a dedicated query string (only when no structured chunks exist)
    query_text = (
        "preventive maintenance tasks inspection intervals lubrication replacement "
        "safety lockout LOTO cleaning filter belt bearing grease oil schedule checklist"
    )
    try:
        query_chunk = [TextChunk(
            chunk_id="maint_query", text=query_text,
            page_start=0, page_end=0, char_start=0, char_end=len(query_text),
            source_file="query", chunk_type="text",
        )]
        embedded_query = await embed_chunks(query_chunk)
        if not embedded_query:
            return _to_chunk_dicts(embedded[:10])

        top_chunks = await retrieve_top_k(
            query_embedding=embedded_query[0]["embedding"],
            manual_id=manual_id,
            top_k=settings.rag_top_k,
        )
        if top_chunks:
            logger.info("[%s] Retrieved %d chunks via dedicated query embedding", manual_id, len(top_chunks))
            return top_chunks
    except Exception as exc:
        logger.warning("[%s] Semantic retrieval failed, using positional fallback: %s", manual_id, exc)

    return _to_chunk_dicts(embedded[:10])


def _retrieve_8_pass_local(
    chunks: list[TextChunk],
    interval_hints: list[int],
) -> list[dict]:
    """
    8-pass retrieval for local dev (no Azure AI Search).
    Mirrors the production strategy but filters by content_type + chunk_type
    instead of running semantic queries against Azure AI Search.

    Production equivalent: each pass becomes a separate hybrid BM25+semantic
    query against the Azure AI Search index with a content_type filter.
    """
    selected: list[dict] = []
    seen_ids: set[str] = set()

    def _chunk_to_dict(c: TextChunk) -> dict:
        return {
            "text": c.text,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "section": c.section_heading,
            "content_type": c.content_type,
            "source_file": c.source_file,
            "chunk_id": c.chunk_id,
            "manual_id": c.manual_id,
            "manual_version": c.manual_version,
        }

    def _add(subset: list[TextChunk], max_n: int) -> None:
        for c in subset:
            if len([x for x in selected if x.get("content_type") == c.content_type]) >= max_n:
                break
            if c.chunk_id not in seen_ids:
                seen_ids.add(c.chunk_id)
                selected.append(_chunk_to_dict(c))

    by_content: dict[str, list[TextChunk]] = {}
    for c in chunks:
        by_content.setdefault(c.content_type, []).append(c)

    for pass_name, content_type, max_n in _RETRIEVAL_PASSES:
        if content_type == "table":
            # Pass 4 — interval tasks: table_row + checkbox chunks matching detected intervals
            interval_chunks = [
                c for c in chunks
                if c.chunk_type in ("table_row", "checkbox")
                and c.chunk_id not in seen_ids
            ]
            # Prioritise chunks whose interval_hint matches what we're looking for
            interval_chunks.sort(
                key=lambda c: (c.interval_hint not in interval_hints if interval_hints else False,
                               c.chunk_type != "table_row"),
            )
            _add(interval_chunks, max_n)
        elif content_type == "procedure":
            # Pass 8 — coverage sweep: best remaining section/paragraph chunks
            remaining = [
                c for c in chunks
                if c.chunk_id not in seen_ids
                and c.chunk_type in ("section", "paragraph", "checkbox")
            ]
            _add(remaining, max_n)
        else:
            _add(by_content.get(content_type, []), max_n)

        logger.debug("[8-pass] %s → %d total selected so far", pass_name, len(selected))

    logger.info("[8-pass] Final: %d chunks from %d passes", len(selected), len(_RETRIEVAL_PASSES))
    return selected


def _select_top_chunks_by_type(chunks: list, top_k: int) -> list[dict]:
    """
    Legacy local dev fast path — kept as ultimate fallback if 8-pass returns nothing.
    Priority: table_row → checkbox → section → paragraph → text
    """
    order = ("table_row", "checkbox", "section", "paragraph", "text")
    by_type: dict[str, list] = {}
    for c in chunks:
        by_type.setdefault(c.chunk_type, []).append(c)
    selected = []
    for t in order:
        selected.extend(by_type.get(t, []))
        if len(selected) >= top_k:
            break
    return [
        {"text": c.text, "page_start": c.page_start, "page_end": c.page_end,
         "section": getattr(c, "section_heading", ""), "content_type": getattr(c, "content_type", "procedure"),
         "source_file": c.source_file, "chunk_id": c.chunk_id,
         "manual_id": getattr(c, "manual_id", ""), "manual_version": getattr(c, "manual_version", "")}
        for c in selected[:top_k]
    ]


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
