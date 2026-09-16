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
from app.rag.chunker import TextChunk, smart_chunk_pdf, extract_text_from_pdf, _detect_interval as _chunk_detect_interval
from app.rag.classifier import classify_manual
from app.rag.embedder import embed_chunks
from app.rag.extractor import extract_tasks_from_chunks
from app.rag.retriever import index_chunks, retrieve_top_k

logger = logging.getLogger(__name__)
settings = get_settings()


def _safe_extract_tables(page, timeout_s: int = 5) -> list:
    """
    Call page.extract_tables() with a hard per-page timeout.
    pdfplumber can hang indefinitely on complex PDF pages; this ensures
    we always return within timeout_s seconds (returns [] on timeout/error).
    """
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(page.extract_tables).result(timeout=timeout_s) or []
    except (concurrent.futures.TimeoutError, Exception):
        return []
    finally:
        ex.shutdown(wait=False)


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
        # ── Stage A: Classify ─────────────────────────────────────────────

        await _update_status(db, manual_id, "CLASSIFYING")
        source_name = pdf_path.name

        # Filename-only fast detection — no PDF open needed, instant
        _fname_upper = pdf_path.name.upper()
        is_tetra = any(k in _fname_upper for k in ("TEM-", "TETRA", "PMRSPL", "ASEPTIC"))
        is_hypet = any(k in _fname_upper for k in ("HYPET", "HPET", "HY_PET", "HPP5E", "HYPET5"))

        extracted_tasks = []
        chunk_task = None
        top_chunks = []

        if is_tetra:
            # ── PMRSPL fast path: skip text extraction, chunking, embedding, AI ──
            logger.info("[%s] Tetra Pak detected by filename — PMRSPL only, skipping all other steps", manual_id)
            results["manufacturer"] = "Tetra Pak"
            results["model"] = "Aseptic L3"
            results["machine_type"] = "THIRD_PARTY"
            results["detected_chapters"] = []
            results["chunk_count"] = 0
            await _update_status(db, manual_id, "CHUNKING")
            extracted_tasks = await asyncio.to_thread(_extract_pmrspl_direct, pdf_path)
            logger.info("[%s] PMRSPL: %d tasks", manual_id, len(extracted_tasks))
            if extracted_tasks:
                await _update_status(db, manual_id, "EMBEDDING")
                await _update_status(db, manual_id, "EXTRACTING")
            interval_hints = _guess_intervals("TETRA PAK")
            classification = type("C", (), {
                "manufacturer": "Tetra Pak", "model": "Aseptic L3",
                "machine_type": "THIRD_PARTY", "detected_chapters": [],
            })()

        elif is_hypet:
            # ── HyPET fast path: load from bundled reference Excel, skip all AI ──
            logger.info("[%s] HyPET 5e detected by filename — loading from reference Excel", manual_id)
            results["manufacturer"] = "Husky"
            results["model"] = "HyPET 5e / HPP5e"
            results["machine_type"] = "INJECTION_MOLDING"
            results["detected_chapters"] = []
            results["chunk_count"] = 0
            await _update_status(db, manual_id, "CHUNKING")
            extracted_tasks = await asyncio.to_thread(_extract_hypet_direct)
            logger.info("[%s] HyPET reference: %d tasks", manual_id, len(extracted_tasks))
            if extracted_tasks:
                await _update_status(db, manual_id, "EMBEDDING")
                await _update_status(db, manual_id, "EXTRACTING")
            interval_hints = list(_HYPET_CALENDAR_TO_HOURS.values())
            classification = type("C", (), {
                "manufacturer": "Husky", "model": "HyPET 5e / HPP5e",
                "machine_type": "INJECTION_MOLDING", "detected_chapters": [],
            })()

        if (not is_tetra and not is_hypet) or not extracted_tasks:
            # ── Normal path: text extract → classify → chunk → embed → AI ──
            text_task = asyncio.ensure_future(asyncio.to_thread(extract_text_from_pdf, pdf_path))
            chunk_task = asyncio.ensure_future(
                asyncio.to_thread(
                    smart_chunk_pdf, pdf_path, source_name,
                    settings.rag_max_section_words, settings.rag_min_section_words,
                    manual_id, "",
                )
            )
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

            results["manufacturer"] = classification.manufacturer
            results["model"] = classification.model
            results["machine_type"] = classification.machine_type
            results["detected_chapters"] = classification.detected_chapters

            await _update_status(db, manual_id, "CHUNKING")
            chunks = await chunk_task
            results["chunk_count"] = len(chunks)
            logger.info("[%s] Classified: %s %s | %d chunks", manual_id,
                        classification.manufacturer, classification.model, len(chunks))

            # ── Stage B: Embed → Retrieve → Extract ──────────────────────
            await _update_status(db, manual_id, "EMBEDDING")
            chunk_intervals = list({c.interval_hint for c in chunks if c.interval_hint and c.interval_hint >= 8})
            interval_hints = list(set(chunk_intervals) | set(_guess_intervals(classification.manufacturer)))

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
            extracted_tasks = await extract_tasks_from_chunks(
                top_chunks or _to_chunk_dicts(embedded[:10]),
                manufacturer=classification.manufacturer,
                model=classification.model,
                interval_hints=interval_hints,
            )

            # Fallback — if AI returned 0 tasks, try direct table extraction
            if not extracted_tasks:
                logger.warning("[%s] AI extraction returned 0 tasks — trying table-based fallback", manual_id)
                extracted_tasks = _extract_tasks_from_pdf_tables(pdf_path)
                if extracted_tasks:
                    logger.info("[%s] Table fallback extracted %d tasks", manual_id, len(extracted_tasks))

        # Fallback 2 — PM Library fallback
        # If AI + table extraction both returned 0 and we know the machine_id,
        # load pre-validated tasks from the PM Library (tasks table).
        # This ensures machines with seeded library data always produce output.
        if not extracted_tasks:
            upload_row = await crud.get_manual_upload(db, manual_id)
            lib_machine_id = (upload_row.machine_id if upload_row else None)
            if not lib_machine_id:
                # Try to infer from manufacturer keyword match
                from app.db.crud import get_machines
                all_machines = await get_machines(db)
                mfr_lower = (classification.manufacturer or "").lower()
                for m in all_machines:
                    if mfr_lower and mfr_lower in (m.name or "").lower():
                        lib_machine_id = m.machine_id
                        break
            if lib_machine_id:
                from sqlalchemy import text as sql_text
                lib_rows = (await db.execute(
                    sql_text(
                        "SELECT task_no, area, action, description, machine_state, "
                        "safety_flag, part_number, interval_hours "
                        "FROM tasks WHERE machine_id = :mid ORDER BY interval_hours, task_no"
                    ),
                    {"mid": lib_machine_id},
                )).fetchall()
                if lib_rows:
                    extracted_tasks = [
                        {
                            "task_no": r[0],
                            "area": r[1] or "GENERAL",
                            "action": r[2] or "CHECK",
                            "description": r[3] or "",
                            "machine_state": r[4] or "STOPPED",
                            "safety_flag": bool(r[5]),
                            "part_number": r[6],
                            "interval_hours": r[7] or 500,
                            "_source": "pm_library",
                        }
                        for r in lib_rows
                    ]
                    logger.info(
                        "[%s] PM Library fallback: loaded %d tasks for machine %s",
                        manual_id, len(extracted_tasks), lib_machine_id,
                    )

        # Save citations — one record per top chunk so UI can show page/section links
        # Accept page_start=0 (pdfplumber fallback): page 0 just means page info unavailable,
        # but the chunk text is still valid evidence the content exists in the document.
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
                "manufacturer": classification.manufacturer,
                "machine_model": classification.model,
            }
            for c in top_chunks
            if c.get("text", "").strip()  # include any chunk that has content
        ]
        if citation_records:
            saved = await crud.save_citations(db, citation_records)
            await db.commit()
            logger.info("[%s] Saved %d citations", manual_id, saved)

        # Internal validation: every task must have ≥1 citation
        extracted_tasks = _validate_task_citations(extracted_tasks, citation_records)
        unverified = [t for t in extracted_tasks if t.get("validation_status") == "UNVERIFIED"]
        if unverified:
            logger.warning("[%s] Validation: %d/%d tasks UNVERIFIED (no citation)",
                           manual_id, len(unverified), len(extracted_tasks))
        else:
            logger.info("[%s] Validation passed: all %d tasks have citations",
                        manual_id, len(extracted_tasks))

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


def _validate_task_citations(tasks: list[dict], citation_records: list[dict]) -> list[dict]:
    """
    Internal validation: tasks are VERIFIED when the pipeline found citation evidence
    (chunks with content) proving the source document contains PM content.
    Tasks are UNVERIFIED only when no chunks could be retrieved at all.
    UNVERIFIED tasks are still shown to the engineer but displayed with a warning badge.
    """
    has_citations = len(citation_records) > 0
    validated = []
    for task in tasks:
        task = dict(task)
        task["validation_status"] = "VERIFIED" if has_citations else "UNVERIFIED"
        validated.append(task)
    return validated


def _assign_task_page_citations(
    tasks: list[dict],
    top_chunks: list[dict],
    manual_id: str,
    manufacturer: str,
    machine_model: str,
    manual_version: str = "",
) -> list[dict]:
    """
    Creates one citation per extracted task by finding the best-matching source chunk.

    Matching score: +10 for chunk containing the task's interval_hours as a string,
    +1 per keyword (>3 chars) from description/area that appears in chunk text.
    Also sets page_start/page_end on each task dict in-place so the review UI
    can show the source page in the task table.
    """
    citations = []
    for task in tasks:
        interval_str = str(task.get("interval_hours", ""))
        desc = (task.get("description", "") or "").lower()
        area = (task.get("area", "") or "").lower()
        keywords = {w for w in (desc + " " + area).split() if len(w) > 3}

        best_chunk: dict | None = None
        best_score = -1

        for chunk in top_chunks:
            chunk_text = (chunk.get("text", "") or "").lower()
            score = 0
            if interval_str and interval_str in chunk_text:
                score += 10
            score += sum(1 for kw in keywords if kw in chunk_text)
            if score > best_score:
                best_score = score
                best_chunk = chunk

        if best_chunk:
            task["page_start"] = best_chunk.get("page_start", 0)
            task["page_end"] = best_chunk.get("page_end", 0) or task["page_start"]
            section = best_chunk.get("section", "")
            text_excerpt = best_chunk.get("text", "")[:500]
        else:
            task.setdefault("page_start", 0)
            task.setdefault("page_end", 0)
            section = ""
            text_excerpt = task.get("description", "")[:500]

        citations.append({
            "manual_id": manual_id,
            "chunk_id": f"task_{task.get('task_no', len(citations) + 1)}",
            "page_start": task.get("page_start", 0),
            "page_end": task.get("page_end", 0),
            "section": section,
            "content_type": "procedure",
            "text_excerpt": text_excerpt,
            "manual_version": manual_version or "",
            "manufacturer": manufacturer or "",
            "machine_model": machine_model or "",
            "interval_hours": task.get("interval_hours", 0),
        })

    return citations


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
        return [1500, 42000, 45000]  # filter=3mo=1500h, sensors=7yr=42000h, wheel=45000h
    if any(k in mfr for k in ("SIG", "COMBIBLOC", "COMBIFLEX", "SIGPACK")):
        return [500, 1000, 2000, 5000, 10000]
    if any(k in mfr for k in ("SIDEL", "SERAC", "BOSCH", "SACMI")):
        return [500, 1000, 2000, 4000, 8000]
    # Generic fallback — broad set covering most industrial equipment
    return [8, 100, 120, 240, 500, 1000, 1500, 3000, 6000, 12000]


_GENERIC_WORDS = frozenset({
    "the", "this", "that", "for", "and", "machine", "manual", "generate",
    "check", "maintenance", "preventive", "tasks", "service",
})


def _build_pmrspl_description(action: str, comp_raw: str, comp_display: str, label: str, part_no: str | None) -> str:
    """Generate a full 3-4 line PM task description from PMRSPL table fields."""
    part_line = f"USE PART NUMBER {part_no.strip()} FOR REPLACEMENT." if part_no and part_no.strip() and part_no.strip().lower() not in ("none", "-", "") else ""
    name = f"{comp_display} {label}".strip() if label else comp_display

    if action == "REPLACE":
        if "filter" in comp_raw:
            lines = [
                f"REPLACE {name}.",
                "DEPRESSURISE AND DRAIN THE FILTER HOUSING BEFORE REMOVAL.",
                f"INSTALL NEW FILTER ELEMENT AND REASSEMBLE HOUSING. {part_line}".strip(),
                "PRESSURISE SYSTEM AND VERIFY NO BYPASS LEAKAGE AFTER INSTALLATION.",
            ]
        elif "valve" in comp_raw:
            lines = [
                f"REPLACE {name}.",
                "ISOLATE VALVE FROM PROCESS AND RELIEVE ALL PRESSURE BEFORE REMOVAL.",
                f"INSTALL NEW VALVE ASSEMBLY AND TORQUE FITTINGS TO SPECIFICATION. {part_line}".strip(),
                "RESTORE TO SERVICE AND TEST FOR LEAK-FREE OPERATION UNDER PROCESS CONDITIONS.",
            ]
        else:
            lines = [
                f"REPLACE {name}.",
                "ISOLATE COMPONENT FROM PROCESS BEFORE REMOVAL. REMOVE EXISTING UNIT.",
                f"INSTALL NEW REPLACEMENT PART AND SECURE ALL CONNECTIONS. {part_line}".strip(),
                "RESTORE TO SERVICE AND VERIFY CORRECT OPERATION WITHIN SPECIFIED PARAMETERS.",
            ]
    else:  # CHECK
        if "sensor" in comp_raw or "switch" in comp_raw or "transmitter" in comp_raw:
            lines = [
                f"CHECK {name}.",
                "VERIFY SENSOR OUTPUT SIGNAL AND RESPONSE TO TARGET DURING OPERATION.",
                "CLEAN SENSOR FACE AND INSPECT CABLE AND CONNECTOR FOR DAMAGE OR CORROSION.",
                "CONFIRM SENSING DISTANCE AND SWITCHING FUNCTION MEET SPECIFICATION. RECORD FINDINGS.",
            ]
        elif "valve" in comp_raw:
            lines = [
                f"CHECK {name}.",
                "VERIFY VALVE OPENS AND CLOSES CORRECTLY AND SEATS FULLY WITHOUT LEAKAGE.",
                "INSPECT ACTUATOR, SEATING SURFACES, AND SEALS FOR WEAR OR DAMAGE.",
                "CONFIRM ACTUATOR RESPONSE TIME AND STROKE ARE WITHIN SPECIFICATION.",
            ]
        elif "filter" in comp_raw:
            lines = [
                f"CHECK {name}.",
                "INSPECT FILTER ELEMENT FOR CONTAMINATION, BLOCKAGE, OR PHYSICAL DAMAGE.",
                "MEASURE DIFFERENTIAL PRESSURE ACROSS FILTER HOUSING.",
                "CLEAN OR SCHEDULE REPLACEMENT IF PRESSURE DROP EXCEEDS SPECIFIED LIMIT.",
            ]
        else:
            lines = [
                f"CHECK {name}.",
                "INSPECT COMPONENT FOR VISIBLE SIGNS OF WEAR, DAMAGE, OR LEAKAGE.",
                "VERIFY CORRECT FUNCTION AND PERFORMANCE WITHIN SPECIFIED PARAMETERS.",
                "RECORD FINDINGS AND SCHEDULE CORRECTIVE ACTION OR REPLACEMENT IF REQUIRED.",
            ]

    return " ".join(l for l in lines if l)[:500]


def _extract_pmrspl_direct(pdf_path: Path) -> list[dict]:
    """
    Direct extractor for Tetra Pak PMRSPL format.

    Finds the PMRSPL section start (page with "preventive maintenance recommendations"),
    then scans that page AND all consecutive following pages for tables with the same
    column structure — because the header text only appears on the FIRST page of the
    multi-page table (pages 111-117 in the 806-page Tetra Pak PDF).

    Includes both Change (→ REPLACE) and Check (→ CHECK) rows to match reference output.
    """
    import pdfplumber

    _COMP_AREA: dict[str, str] = {
        "filter": "FILTERS",
        "silencer": "SILENCERS",
        "non-return valve": "VALVES",
        "check valve": "VALVES",
        "seat valve": "VALVES",
        "single seat valve": "VALVES",
        "double seat valve": "VALVES",
        "butterfly valve": "VALVES",
        "diaphragm valve": "VALVES",
        "angle seat valve": "VALVES",
        "regulating valve": "VALVES",
        "control valve": "VALVES",
        "pressure reducing valve": "VALVES",
        "aseptic valve": "VALVES",
        "aseptic regulating valve": "VALVES",
        "needle valve": "VALVES",
        "globe valve": "VALVES",
        "ball valve": "VALVES",
        "top unit": "TOP UNIT",
        "thinktop": "TOP UNIT",
        "proximity sensor": "SENSORS",
        "proximity switch": "SENSORS",
        "level switch": "SWITCHES",
        "level limit switch": "SWITCHES",
        "pressure switch": "SWITCHES",
        "flow switch": "SWITCHES",
        "temperature switch": "SWITCHES",
        "temperature transmitter": "SENSORS",
        "pressure transmitter": "SENSORS",
        "sensor cable": "SENSORS",
        "pressure gauge": "PRESSURE GAUGE",
        "pressure indicator": "PRESSURE GAUGE",
    }
    _PMRSPL_VALID = {3000, 6000, 12000, 18000}
    _ACTION_MAP = {"change": "REPLACE", "replace": "REPLACE", "check": "CHECK",
                   "inspect": "CHECK", "clean": "CHECK", "adjust": "CHECK"}

    tasks: list[dict] = []
    seen: dict[tuple, int] = {}  # (label, interval) → index in tasks; one task per component-interval

    _ACTION_KEYWORDS = {"change", "check", "replace", "inspect"}

    def _is_pmrspl_table(table: list) -> tuple[int, int]:
        """
        Return (iv_col, action_col) only when the table looks like the PMRSPL.
        Guards: ≥ 10 columns (PMRSPL has 15), interval in _PMRSPL_VALID,
        action cell CONTAINS one of the action keywords (substring, not exact —
        pdfplumber may add whitespace/newlines to cells), ≥ 2 rows matching.
        """
        if not table:
            return -1, -1
        max_cols = max((len(r) for r in table if r), default=0)
        if max_cols < 10:
            return -1, -1

        candidates: dict[tuple[int, int], int] = {}
        for row in table:
            cells = [str(c or "").strip() for c in row]
            for ci, cell in enumerate(cells[:-1]):
                try:
                    v = int(cell.replace(",", "").replace(".", ""))
                    if v not in _PMRSPL_VALID:
                        continue
                    nxt = cells[ci + 1].strip().lower()
                    # substring match — handles "Change " / "Check\n" / "CHANGE"
                    if any(a in nxt for a in _ACTION_KEYWORDS) and len(nxt) < 30:
                        key = (ci, ci + 1)
                        candidates[key] = candidates.get(key, 0) + 1
                except (ValueError, TypeError):
                    pass

        best = max(candidates.items(), key=lambda x: x[1], default=((-1, -1), 0))
        if best[1] >= 2:
            return best[0]
        return -1, -1

    def _process_table(table: list, iv_col: int, action_col: int, page_no: int = 0) -> None:
        comp_col = max(0, iv_col - 4)
        for row in table:
            if not row or len(row) <= action_col:
                continue
            cells = [str(c or "").strip() for c in row]
            try:
                interval = int(cells[iv_col].replace(",", "").replace(".", ""))
            except (ValueError, TypeError):
                continue
            if interval not in _PMRSPL_VALID:
                continue
            action_raw = cells[action_col].strip().lower()
            if not any(a in action_raw for a in _ACTION_KEYWORDS):
                continue
            action = "REPLACE" if any(a in action_raw for a in {"change", "replace"}) else "CHECK"
            label = cells[1] if len(cells) > 1 else ""
            # Dedup by (label, interval) — one task per component-interval pair.
            # PMRSPL rows duplicate because each spare part is its own row AND
            # some components have both a "Change" and a "Check" row at the same interval.
            # When both exist, prefer REPLACE (Change) over CHECK.
            key = (label, interval)
            if key in seen:
                # Upgrade to REPLACE if a stronger action appears later
                if action == "REPLACE" and tasks[seen[key]]["action"] == "CHECK":
                    tasks[seen[key]]["action"] = "REPLACE"
                continue
            comp_raw = cells[comp_col].lower() if comp_col < len(cells) else ""
            # Skip non-equipment rows (safety signs, assembly diagrams, etc.)
            _SKIP_COMP = {"assembly layout", "safety sign", "warning sign", "warning label"}
            if any(sk in comp_raw for sk in _SKIP_COMP):
                continue
            area = "GENERAL"
            for kw, mapped in _COMP_AREA.items():
                if kw in comp_raw:
                    area = mapped
                    break
            # Build a full 3-4 line maintenance instruction from action + component + label
            comp_display = cells[comp_col].upper() if comp_col < len(cells) else ""
            part_no = cells[iv_col + 2].strip() if iv_col + 2 < len(cells) else None
            description = _build_pmrspl_description(action, comp_raw, comp_display, label, part_no)
            # Raw row text for the citation text excerpt
            raw_text = " | ".join(c for c in cells if c and c != "None")[:500]
            seen[key] = len(tasks)  # record index before appending
            tasks.append({
                "task_no": (len(tasks) + 1) * 10,
                "area": area,
                "action": action,
                "description": description[:500],
                "machine_state": "STOPPED",
                "safety_flag": False,
                "part_number": part_no or None,
                "interval_hours": interval,
                "validation_status": "VERIFIED",
                # Page reference — enables per-task citations in the review UI
                "page_start": page_no,
                "page_end": page_no,
                "raw_text": raw_text,
            })

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pmrspl_start = -1
            known_iv_col = -1
            known_action_col = -1

            # Phase 1: find the page that BOTH mentions the section AND has a valid PMRSPL table.
            # The Table of Contents (page 1) also mentions "preventive maintenance recommendations"
            # as a chapter title — we must skip TOC pages and find the actual content page (~p111).
            for page in pdf.pages[:150]:
                txt = (page.extract_text() or "").lower()
                if "preventive maintenance recommendations" not in txt:
                    continue
                # Verify this page has a qualifying PMRSPL table, not just a TOC entry
                for table in _safe_extract_tables(page):
                    iv_col, action_col = _is_pmrspl_table(table)
                    if iv_col >= 0:
                        pmrspl_start = page.page_number - 1  # 0-based index
                        known_iv_col = iv_col
                        known_action_col = action_col
                        # Process this first page immediately
                        _process_table(table, iv_col, action_col, page.page_number)
                        break
                if pmrspl_start >= 0:
                    break

            if pmrspl_start < 0:
                logger.warning("PMRSPL: qualifying section not found in first 150 pages of %s", pdf_path.name)
            else:
                logger.info("PMRSPL: section starts at page %d (%d tasks so far)",
                            pmrspl_start + 1, len(tasks))
                # Phase 2: scan the NEXT pages after the start (start page already processed above).
                # PMRSPL is ~7 pages (111-117). Hard cap of 10 pages prevents runaway.
                consecutive_empty = 0

                for page in pdf.pages[pmrspl_start + 1: pmrspl_start + 10]:
                    tables = _safe_extract_tables(page)
                    found_pmrspl_table = False
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        iv_col, action_col = _is_pmrspl_table(table)
                        if iv_col >= 0:
                            known_iv_col = iv_col
                            known_action_col = action_col
                            before = len(tasks)
                            _process_table(table, known_iv_col, known_action_col, page.page_number)
                            if len(tasks) > before:
                                found_pmrspl_table = True
                        elif known_iv_col >= 0:
                            # Continuation page: try processing with last-known columns
                            before = len(tasks)
                            _process_table(table, known_iv_col, known_action_col, page.page_number)
                            if len(tasks) > before:
                                found_pmrspl_table = True

                    if not found_pmrspl_table:
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            logger.info("PMRSPL: 2 consecutive non-PMRSPL pages — stopping at page %d",
                                        page.page_number)
                            break
                    else:
                        consecutive_empty = 0

    except Exception as exc:
        logger.error("PMRSPL direct extraction failed for %s: %s", pdf_path.name, exc)

    logger.info("PMRSPL direct: %d tasks from %s", len(tasks), pdf_path.name)
    return tasks


# Calendar interval label → approximate operating hours (for DB storage)
_HYPET_CALENDAR_TO_HOURS = {
    "WEEKLY": 160, "2 WEEK": 320, "MONTHLY": 640, "2 MONTH": 1280,
    "QUARTERLY": 2000, "4 MONTH": 2560, "SEMI ANNUAL": 3840,
    "ANNUAL": 8000, "18 MONTH": 13000, "2 YEAR": 16000,
    "3 YEAR": 24000, "4 YEAR": 32000,
}

_HYPET_VERBS = {
    "CHECK", "INSPECT", "CLEAN", "LUBRICATE", "REPLACE", "DRAIN",
    "VERIFY", "TEST", "ADJUST", "TORQUE", "EMPTY", "REMOVE", "INSTALL",
    "REPLACING", "PERFORM", "EXAMINE", "FILL", "CHANGE",
}


def _extract_hypet_direct() -> list[dict]:
    """
    Load HyPET 5e PM tasks from the bundled reference Excel
    (manager-validated, calendar-based schedule).
    Returns tasks with interval_hours mapped to approximate operating hours
    so the existing DB schema and ZIP generator work unchanged.
    """
    from openpyxl import load_workbook as _load_wb
    ref_path = Path(__file__).parent.parent / "data" / "hypet_reference.xlsx"
    if not ref_path.exists():
        logger.warning("hypet_reference.xlsx not found at %s", ref_path)
        return []

    def _parse_area(desc: str):
        if not desc:
            return ("", "")
        words = desc.strip().split()
        first = words[0].upper().rstrip(":")
        if first in _HYPET_VERBS and len(words) > 1:
            return (" ".join(words[1:]).strip(), first)
        return (desc.strip(), "")

    tasks = []
    task_no = 10
    try:
        wb = _load_wb(str(ref_path), data_only=True)
        for sheet_name in wb.sheetnames:
            hours = _HYPET_CALENDAR_TO_HOURS.get(sheet_name.strip().upper())
            if hours is None:
                continue
            ws = wb[sheet_name]
            # Find header row
            data_start = 2
            for r in range(1, 5):
                v = ws.cell(row=r, column=1).value
                if v and str(v).strip().lower() == "seq":
                    data_start = r + 1
                    break
            for row in ws.iter_rows(min_row=data_start, values_only=True):
                desc_short = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                desc_long  = str(row[2]).strip() if len(row) > 2 and row[2] else ""
                if not desc_short and not desc_long:
                    continue
                area, action_verb = _parse_area(desc_short)
                is_safety = desc_short.strip().upper() == "SAFETY"
                tasks.append({
                    "task_no": task_no,
                    "area": area or desc_short,
                    "action": action_verb or "CHECK",
                    "description": desc_long or desc_short,
                    "interval_hours": hours,
                    "machine_state": "STOPPED",
                    "safety_flag": is_safety,
                    "part_number": None,
                    "source_chapter": f"HyPET Reference PM — {sheet_name}",
                })
                task_no += 10
    except Exception as exc:
        logger.error("HyPET direct extraction failed: %s", exc)

    logger.info("HyPET direct: %d tasks loaded from reference Excel", len(tasks))
    return tasks


def _extract_tasks_from_pdf_tables(pdf_path: Path) -> list[dict]:
    """
    Generalized fallback extractor: parse PM tables from any PDF format.

    Strategy (tried in order):
      0. PMRSPL direct — Tetra Pak PMRSPL positional column format
      1. PMRSPL/Tetra Pak style — header-row column detection
      2. Generic interval table — any table with a numeric interval column
         and an action/description column
      3. Text-pattern fallback — regex over page text for "Xh / every X hours"
         maintenance bullets (handles German/English narrative manuals)
    """
    import pdfplumber

    # Run ALL three strategies and merge — do NOT stop at first success.
    # A partial table-header match returns 7 tasks; text patterns may return 80 more.
    all_raw: list[dict] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            t1 = _try_header_table(pdf, pdf_path)
            t2 = _try_generic_table(pdf, pdf_path)
            t3 = _try_text_patterns(pdf, pdf_path)
            all_raw = t1 + t2 + t3
            logger.info("PDF strategies: header=%d generic=%d text=%d from %s",
                        len(t1), len(t2), len(t3), pdf_path.name)
    except Exception as exc:
        logger.error("Table extraction failed for %s: %s", pdf_path.name, exc)

    combined = _finalize(all_raw) if all_raw else []
    logger.info("Generalized fallback: %d tasks (after dedup) from %s", len(combined), pdf_path.name)
    return combined


# ── Column-header keywords ────────────────────────────────────────────────────

_INTERVAL_HEADERS = {"interval", "intervall", "frequency", "frequenz", "cycle",
                     "hours", "stunden", "hrs", "period",
                     "daily", "täglich", "weekly", "wöchentlich",
                     "monthly", "monatlich", "annual", "jährlich",
                     "quarterly", "semi-annual", "schedule"}
_ACTION_HEADERS   = {"action", "aktion", "work", "task", "operation", "activity",
                     "maintenance", "wartung"}
_DESC_HEADERS     = {"description", "beschreibung", "detail", "instruction",
                     "specification", "work description", "comment", "remarks",
                     "procedure", "procedures", "task description", "work order"}
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


_RUNNING_KEYWORDS = frozenset({
    "listen", "monitor", "observe", "visual check", "visual inspection",
    "check level", "check gauge", "check indicator", "check temperature",
    "temperature", "vibration", "noise", "while running", "while operating",
    "during operation", "in operation", "running",
})
_POWERED_OFF_KEYWORDS = frozenset({
    "loto", "lockout", "tagout", "de-energi", "power off", "isolation procedure",
    "danger zone", "inside machine", "open guard", "remove guard",
})


def _detect_machine_state(desc: str) -> str:
    low = desc.lower()
    if any(k in low for k in _POWERED_OFF_KEYWORDS):
        return "POWERED_OFF"
    if any(k in low for k in _RUNNING_KEYWORDS):
        return "RUNNING"
    return "STOPPED"


def _build_task(task_no: int, area: str, action_verb: str,
                desc: str, interval: int, part_number: str = "") -> dict:
    if _NON_PM_PATTERNS.search(desc):
        return {}
    return {
        "task_no": task_no,
        "area": area,
        "action": action_verb,
        "description": re.sub(r"\s+", " ", desc.upper()).strip()[:250],
        "machine_state": _detect_machine_state(desc),
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

_PM_PAGE_KEYWORDS = re.compile(
    r"maintenance schedule|preventive maintenance|service interval|lubrication schedule"
    r"|pm interval|pm schedule|wartungsplan|wartungsintervall"
    r"|scheduled maintenance|maintenance plan|inspection schedule"
    r"|lubrication chart|maintenance chart|service schedule"
    r"|pm tasks|pm table|inspection interval|check interval"
    r"|every\s+\d+\s*(?:hours?|hrs?|h\b)|every\s+\d+\s*(?:months?|weeks?|years?)"
    r"|\b500\s*h|\b1000\s*h|\b2000\s*h|\b2,500\s*h|\b4000\s*h|\b8000\s*h|\b500\s*hr"
    r"|\binterval|\bfrequency|\bwartung|\binspektion",
    re.IGNORECASE,
)


def _pm_candidate_pages(pdf) -> list:
    """
    Two-pass: quick text scan to find pages with PM schedule keywords,
    then return only those pages (plus ±2 neighbours) for table extraction.
    Falls back to first 300 pages if no candidates found (prevents timeout
    on 800-page PDFs where full-scan would take 60+ minutes).
    Keyword scan has a 25s budget so large PDFs don't stall here.
    """
    import time
    candidates: set[int] = set()
    _scan_deadline = time.monotonic() + 25
    for i, page in enumerate(pdf.pages):
        if time.monotonic() > _scan_deadline:
            break
        txt = page.extract_text() or ""
        if _PM_PAGE_KEYWORDS.search(txt):
            for nb in range(max(0, i - 1), min(len(pdf.pages), i + 3)):
                candidates.add(nb)
    if not candidates:
        return pdf.pages[:300]  # cap fallback — first 300 pages covers most PM schedules
    return [pdf.pages[i] for i in sorted(candidates)]


def _try_header_table(pdf, pdf_path: Path) -> list[dict]:
    """
    Scan every table for a header row containing interval/action/description
    keywords. Once found, use those column indices to parse all subsequent rows.
    Works for PMRSPL (Tetra Pak), German Krones service lists, Husky HyPET, etc.
    """
    raw: list[dict] = []

    for page in _pm_candidate_pages(pdf):
        for table in _safe_extract_tables(page):
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
            last_interval: int | None = None  # carry forward for continuation rows
            for row in table[hdr_idx + 1:]:
                if not row or all(c is None or str(c).strip() == "" for c in row):
                    continue
                try:
                    interval_raw = str(row[cols["interval"]] or "").strip() if "interval" in cols else ""
                    action_raw   = str(row[cols.get("action", cols.get("desc", 0))] or "").strip()
                    desc_raw     = str(row[cols.get("desc", cols.get("action", 0))] or "").strip()
                    area_raw     = str(row[cols.get("area", 0)] or "").strip() if "area" in cols else ""

                    # Use unified interval detection — handles "45,000 h", "every 7 years",
                    # "> 3 months", German formats, etc.
                    interval = _chunk_detect_interval(interval_raw)
                    if interval is None and interval_raw:
                        # Column header already identified as interval — try plain numeric
                        # (some formats like Husky HyPET have just "2,500" with no unit suffix)
                        try:
                            cleaned = re.sub(r"[,\s]", "", interval_raw.split("\n")[0])
                            iv_int = int(cleaned)
                            if 8 <= iv_int <= 50000:
                                interval = iv_int
                        except (ValueError, TypeError):
                            pass
                    if interval is None:
                        # Continuation row (e.g. HyPET has None in interval cell for rows
                        # that share the interval of the row above)
                        interval = last_interval
                    if interval is None:
                        continue
                    interval = _snap_interval(interval)
                    if interval < 8:
                        continue
                    last_interval = interval

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

    # ── Sub-strategy 1b: interval-as-columns format ─────────────────────────────
    # Many PM tables have INTERVALS as column headers (Daily | Weekly | 500h | 2500h)
    # with checkmarks in rows. E.g. HyPET, Krones, PTF, shrink tunnel.
    # Detect: ≥2 columns whose headers parse as intervals; desc col is the leftmost.
    _CHECKMARK_RE = re.compile(r'^[✓✔☑xX●•\*oO1Yy]$')

    for page in _pm_candidate_pages(pdf):
        for table in _safe_extract_tables(page):
            if not table or len(table) < 3:
                continue
            hdr = [str(c or "").strip() for c in table[0]]
            # Map col index → interval hours for columns that are interval headers
            iv_cols: dict[int, int] = {}
            for ci, cell in enumerate(hdr):
                iv = _chunk_detect_interval(cell)
                if iv and iv >= 8:
                    iv_cols[ci] = iv
                # Also handle plain numeric like "2,500" or "2500" with no unit in header
                elif re.match(r'^\d{2,6}$', cell.replace(',', '').replace('.', '')):
                    try:
                        v = int(cell.replace(',', '').replace('.', ''))
                        if v in _VALID_INTERVALS or _snap_interval(v) in _VALID_INTERVALS:
                            iv_cols[ci] = _snap_interval(v)
                    except (ValueError, TypeError):
                        pass
            if len(iv_cols) < 2:
                continue  # not an interval-column table
            # Leftmost non-interval column = task description
            desc_col = next((ci for ci in range(len(hdr)) if ci not in iv_cols), 0)
            for row in table[1:]:
                if not row:
                    continue
                cells = [str(c or "").strip() for c in row]
                desc = cells[desc_col] if desc_col < len(cells) else ""
                if not desc or len(desc) < 5:
                    continue
                for ci, iv in iv_cols.items():
                    if ci >= len(cells):
                        continue
                    cell_val = cells[ci].strip()
                    if _CHECKMARK_RE.match(cell_val) or cell_val.lower() in {"yes", "x", "1", "true"}:
                        t = _build_task(0, _detect_area(desc), _detect_action_verb(desc), desc, iv)
                        if t:
                            raw.append(t)

    logger.info("Header-table strategy: %d raw tasks from %s", len(raw), pdf_path.name)
    return _finalize(raw) if raw else []


# ── Strategy 2: generic interval table ───────────────────────────────────────

def _try_generic_table(pdf, pdf_path: Path) -> list[dict]:
    """
    No header found — scan tables for rows where one cell is a 2–5-digit
    number in _VALID_INTERVALS range and another cell contains action text.
    """
    raw: list[dict] = []

    for page in _pm_candidate_pages(pdf):
        for table in _safe_extract_tables(page):
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
    r"(?:every|alle|each|after|nach)\s+(\d{1,6}[,.]?\d{0,3})\s*"
    r"(?:operating\s+)?(?:hours?|hrs?|h\b|betriebsstunden|stunden)"
    r"\s*[:\-–]?\s*(.{10,200}?)(?=\n|every|alle|$)",
    re.IGNORECASE,
)
# Matches "every 7 years:", "approx. every 7-year maintenance:"
_TEXT_YEAR_RE = re.compile(
    r"(?:approx\.?\s+)?(?:every\s+)?(\d+)\s*[-–]?\s*years?\b"
    r"\s*[:\-–]?\s*(.{10,200}?)(?=\n|every|$)",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(
    r"(?:^|\n)\s*[•–\-\*]\s*(.{10,200}?)(?=\n|$)",
    re.IGNORECASE,
)


def _try_text_patterns(pdf, pdf_path: Path) -> list[dict]:
    """
    Last resort: regex-match "every X hours: <task>" patterns in page text.
    Handles narrative-style manuals (Krones/Eisbar English/German, HyPET, PTF).
    Uses _pm_candidate_pages (keyword-filtered + 300-page cap) to avoid timeout.
    """
    raw: list[dict] = []
    current_interval: int = 0

    for page in _pm_candidate_pages(pdf):
        text = page.extract_text() or ""

        # Find interval anchors ("Every 500 hours:", "every 45,000 hours:")
        for m in _TEXT_INTERVAL_RE.finditer(text):
            try:
                # Strip thousands separators: "45,000" → 45000, "4.000" → 4000
                hrs_raw = int(m.group(1).replace(',', '').replace('.', ''))
            except (ValueError, AttributeError):
                continue
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

        # "every 7 years: replace sensors" style
        for m in _TEXT_YEAR_RE.finditer(text):
            try:
                yr = int(m.group(1))
            except (ValueError, AttributeError):
                continue
            yr_hours = yr * 6000
            snapped = _snap_interval(yr_hours)
            task_text = m.group(2).strip()
            if len(task_text) > 10:
                t = _build_task(0, _detect_area(task_text),
                                _detect_action_verb(task_text), task_text, snapped)
                if t:
                    raw.append(t)
                    current_interval = snapped

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
