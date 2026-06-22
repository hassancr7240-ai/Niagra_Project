from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile, status

from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.utils.audit import log_action
from app.utils.security import compute_bytes_hash, sanitize_filename

router = APIRouter(prefix="/api/manual", tags=["Manual Upload / RAG"])

_ALLOWED_TYPES = {"application/pdf", "application/octet-stream"}
_MAX_FILE_MB = 50


def _basic_pdf_safety_check(content: bytes) -> bool:
    """
    Basic PDF safety check — blocks only genuinely malicious patterns.
    NOTE: /EmbeddedFile is intentionally excluded — many legitimate Krones/
    eisbär manuals embed fonts and attachments and are NOT malicious.
    Production: replace with Microsoft Defender for Cloud Storage webhook.
    """
    if len(content) < 5:
        return False
    if not content[:4].startswith(b"%PDF"):
        return False
    # Only block active exploit patterns (JavaScript execution)
    exploit_patterns = [b"/JavaScript", b"AA /JS", b"/OpenAction /JS", b"eval("]
    sample = content[:16384]
    for pattern in exploit_patterns:
        if pattern in sample:
            return False
    return True


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_manual(
    background_tasks: BackgroundTasks,
    user: CurrentUserDep,
    db: DBDep,
    file: UploadFile = File(...),
    machine_id: Optional[str] = Form(None),
) -> dict:
    """
    POST /api/manual/upload
    Upload a machine manual PDF. Triggers RAG pipeline asynchronously.

    Pipeline stages (data flow diagram):
    PDF Upload → Pre-Classify → Find Chapter → Chunk → Embed →
    Vector Store → RAG Retrieve → AI Extract → Validate → Pending Review
    """
    user.require("manual:upload")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are accepted",
        )

    content = await file.read()
    if len(content) > _MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {_MAX_FILE_MB}MB limit",
        )

    # ── Virus scan (Architecture: "All uploads virus scanned") ───────────────
    # Production: integrate with Microsoft Defender for Cloud Storage or ClamAV
    # Stub: check for common malicious PDF markers
    _scan_result = _basic_pdf_safety_check(content)
    if not _scan_result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File failed safety check. Upload rejected.",
        )

    file_hash = compute_bytes_hash(content)
    safe_name = sanitize_filename(file.filename)

    # Save to temp file for pipeline
    tmp_dir = tempfile.mkdtemp()
    tmp_path = Path(tmp_dir) / safe_name
    tmp_path.write_bytes(content)

    # Create DB record
    upload_record = await crud.create_manual_upload(
        db,
        {
            "original_filename": safe_name,
            "machine_id": machine_id,
            "local_path": str(tmp_path),
            "file_size_bytes": len(content),
            "status": "UPLOADED",
            "uploaded_by": user.email,
        },
    )

    await log_action(
        db,
        action="MANUAL_UPLOADED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="manual_upload",
        resource_id=upload_record.manual_id,
        details={"filename": safe_name, "size_bytes": len(content), "machine_id": machine_id},
        ip_address=user.ip_address,
    )

    # Commit NOW — release the SQLite write lock before the background task starts.
    # BackgroundTasks run before FastAPI's dependency-generator cleanup, so without
    # this explicit commit the ORM session holds the write lock for the entire pipeline.
    await db.commit()

    # Run pipeline in background
    background_tasks.add_task(
        _run_pipeline_task,
        upload_record.manual_id,
        tmp_path,
    )

    return {
        "manual_id": upload_record.manual_id,
        "filename": safe_name,
        "status": "UPLOADED",
        "message": "Manual uploaded successfully. RAG pipeline started. Check status endpoint for progress.",
    }


@router.get("/uploads/{manual_id}/status")
async def get_upload_status_light(manual_id: str, user: CurrentUserDep, db: DBDep) -> dict:
    """Lightweight status poll — returns just status + progress for the frontend spinner."""
    user.require("manual:upload")
    upload = await crud.get_manual_upload(db, manual_id)
    if not upload:
        return {"manual_id": manual_id, "status": "NOT_FOUND", "ready": False, "progress": 0, "label": "Not found"}

    _status_labels = {
        "UPLOADED":   (10, "Uploaded — starting pipeline…"),
        "CLASSIFYING": (25, "Classifying document…"),
        "CHUNKING":   (45, "Splitting into chunks…"),
        "EMBEDDING":  (65, "Generating embeddings…"),
        "EXTRACTING": (85, "Extracting PM tasks…"),
        "PENDING_REVIEW": (100, "Processing complete — ready to chat!"),
        "APPROVED":   (100, "Approved and added to PM Library"),
        "FAILED":     (0,  "Processing failed"),
    }
    progress, label = _status_labels.get(upload.status, (50, upload.status))
    ready = upload.status in ("PENDING_REVIEW", "APPROVED")
    import json as _json
    task_count = len(_json.loads(upload.extracted_tasks or "[]")) if ready else 0
    return {
        "manual_id": manual_id,
        "status": upload.status,
        "ready": ready,
        "progress": progress,
        "label": label,
        "task_count": task_count,
        "filename": upload.original_filename,
        "error": upload.error_message if upload.status == "FAILED" else None,
    }


@router.get("/uploads", response_model=list[dict])
async def list_uploads(user: CurrentUserDep, db: DBDep) -> list[dict]:
    user.require("manual:upload")
    uploads = await crud.get_manual_uploads(db, limit=50)
    return [
        {
            "manual_id": u.manual_id,
            "filename": u.original_filename,
            "status": u.status,
            "machine_id": u.machine_id,
            "detected_manufacturer": u.detected_manufacturer,
            "task_count": len(json.loads(u.extracted_tasks or "[]")),
            "uploaded_by": u.uploaded_by,
            "created_at": u.created_at.isoformat(),
        }
        for u in uploads
    ]


@router.get("/uploads/{manual_id}", response_model=dict)
async def get_upload_status(manual_id: str, user: CurrentUserDep, db: DBDep) -> dict:
    user.require("manual:upload")
    upload = await crud.get_manual_upload(db, manual_id)
    if not upload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")

    tasks = json.loads(upload.extracted_tasks or "[]")
    return {
        "manual_id": upload.manual_id,
        "filename": upload.original_filename,
        "status": upload.status,
        "machine_id": upload.machine_id,
        "detected_manufacturer": upload.detected_manufacturer,
        "detected_chapters": json.loads(upload.detected_chapters or "[]"),
        "extracted_task_count": len(tasks),
        "extracted_tasks": tasks if upload.status in ("PENDING_REVIEW", "APPROVED") else [],
        "error_message": upload.error_message,
        "uploaded_by": upload.uploaded_by,
        "approved_by": upload.approved_by,
        "created_at": upload.created_at.isoformat(),
    }


@router.post("/uploads/{manual_id}/approve", response_model=dict)
async def approve_extracted_tasks(
    manual_id: str,
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = Form(None),
) -> dict:
    """
    Engineer approves extracted tasks and adds them to the PM Library.
    Step 4 of the RAG Accuracy Layers: Approved → Added to library.
    """
    user.require("manual:approve")

    upload = await crud.get_manual_upload(db, manual_id)
    if not upload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    if upload.status != "PENDING_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload is not pending review (status: {upload.status})",
        )

    target_machine = machine_id or upload.machine_id
    if not target_machine:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="machine_id is required to add tasks to the library",
        )

    tasks = json.loads(upload.extracted_tasks or "[]")
    if not tasks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No extracted tasks to approve",
        )

    machine = await crud.get_machine(db, target_machine)
    if not machine:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Machine '{target_machine}' not found — create the machine first",
        )

    import uuid
    from datetime import datetime
    added_count = 0
    for t in tasks:
        if not t.get("interval_hours"):
            continue
        try:
            await crud.create_task(
                db,
                {
                    "task_id": str(uuid.uuid4()),
                    "machine_id": target_machine,
                    "interval_hours": int(t["interval_hours"]),
                    "task_no": int(t.get("task_no", (added_count + 1) * 10)),
                    "area": str(t.get("area", "GENERAL"))[:64],
                    "action": str(t.get("action", "CHECK"))[:64],
                    "description": str(t.get("description", ""))[:2000],
                    "machine_state": str(t.get("machine_state", "STOPPED")),
                    "safety_flag": bool(t.get("safety_flag", False)),
                    "part_number": t.get("part_number"),
                    "source_chapter": f"RAG Extract — {upload.original_filename}",
                    "source_section": "AI Extracted",
                },
            )
            added_count += 1
        except Exception:
            pass

    await crud.update_manual_upload(
        db,
        manual_id,
        {
            "status": "APPROVED",
            "approved_by": user.email,
            "approved_at": datetime.utcnow(),
            "machine_id": target_machine,
        },
    )

    await log_action(
        db,
        action="MANUAL_APPROVED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="manual_upload",
        resource_id=manual_id,
        details={"machine_id": target_machine, "tasks_added": added_count},
        ip_address=user.ip_address,
    )

    return {
        "manual_id": manual_id,
        "machine_id": target_machine,
        "tasks_added_to_library": added_count,
        "status": "APPROVED",
    }


async def _run_pipeline_task(manual_id: str, pdf_path: Path) -> None:
    """
    Background task wrapper.
    Uses a raw sqlite3 connection (isolation_level=None = autocommit) for status
    updates to avoid SQLAlchemy's connection-pool write-lock contention with the
    main request session on SQLite.
    """
    import asyncio
    import logging
    import shutil
    import sqlite3
    from pathlib import Path as _Path

    log = logging.getLogger(__name__)

    # Path to the SQLite file (same one the ORM uses)
    db_path = str(_Path(__file__).parent.parent.parent.parent / "data" / "pm_automation.db")

    def _raw_update(status: str, error: str = "") -> None:
        """Write status update via raw sqlite3 in autocommit mode — no lock contention."""
        try:
            conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
            if error:
                conn.execute(
                    "UPDATE manual_uploads SET status=?, error_message=?, updated_at=CURRENT_TIMESTAMP WHERE manual_id=?",
                    (status, error[:2000], manual_id),
                )
            else:
                conn.execute(
                    "UPDATE manual_uploads SET status=?, updated_at=CURRENT_TIMESTAMP WHERE manual_id=?",
                    (status, manual_id),
                )
            conn.close()
        except Exception as e:
            log.warning("raw_update failed (%s): %s", status, e)

    def _raw_finalize(extracted_tasks_json: str, manufacturer: str, chapters_json: str) -> None:
        """Write final pipeline results via raw sqlite3."""
        try:
            conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
            conn.execute(
                "UPDATE manual_uploads SET status='PENDING_REVIEW', extracted_tasks=?, detected_manufacturer=?, detected_chapters=?, updated_at=CURRENT_TIMESTAMP WHERE manual_id=?",
                (extracted_tasks_json, manufacturer, chapters_json, manual_id),
            )
            conn.close()
        except Exception as e:
            log.warning("raw_finalize failed: %s", e)

    # Run the actual pipeline work using raw status updates instead of ORM updates
    try:
        await _run_pipeline_direct(manual_id, pdf_path, _raw_update, _raw_finalize)
    except Exception as exc:
        log.error("Background pipeline task failed for %s: %s", manual_id, exc)
        _raw_update("FAILED", str(exc))
    finally:
        parent = pdf_path.parent
        try:
            shutil.rmtree(str(parent), ignore_errors=True)
        except Exception:
            pass


async def _run_pipeline_direct(manual_id: str, pdf_path: Path, update_fn, finalize_fn) -> None:
    """
    Runs the full RAG pipeline calling update_fn(status) for progress updates
    and finalize_fn(tasks_json, manufacturer, chapters_json) when complete.
    Avoids any ORM session for status writes — only uses raw sqlite3 via callbacks.
    """
    import json
    import logging
    from app.rag.chunker import TextChunk, chunk_text, extract_chapter_text, extract_text_from_pdf
    from app.rag.classifier import classify_manual
    from app.rag.embedder import embed_chunks
    from app.rag.extractor import extract_tasks_from_chunks
    from app.rag.retriever import index_chunks, retrieve_top_k
    from app.config import get_settings

    log = logging.getLogger(__name__)
    settings = get_settings()

    update_fn("CLASSIFYING")
    log.info("[%s] Extracting text from PDF", manual_id)
    full_text, page_offsets = extract_text_from_pdf(pdf_path)
    sample_text = full_text[:8000]

    classification = await classify_manual(pdf_path, sample_text)
    log.info("[%s] Classified: %s %s", manual_id, classification.manufacturer, classification.model)

    update_fn("CHUNKING")
    chapter_text = ""
    for ch_num in (classification.detected_chapters or []):
        chapter_text += extract_chapter_text(full_text, ch_num) + "\n"
    if not chapter_text:
        chapter_text = full_text

    chunks = chunk_text(
        chapter_text,
        source_file=pdf_path.name,
        chunk_size=settings.rag_chunk_size,
        overlap=settings.rag_chunk_overlap,
        page_offsets=page_offsets,
    )
    log.info("[%s] Created %d chunks", manual_id, len(chunks))

    update_fn("EMBEDDING")
    embedded = await embed_chunks(chunks)
    log.info("[%s] Embedded %d chunks", manual_id, len(embedded))

    if embedded:
        await index_chunks(embedded, manual_id)

    update_fn("EXTRACTING")
    # Semantic retrieval for best maintenance chunks
    query_text = "maintenance tasks inspection intervals lubrication replacement safety lockout cleaning filter belt schedule"
    query_chunk = [TextChunk(chunk_id="q", text=query_text, page_start=0, page_end=0, char_start=0, char_end=len(query_text), source_file="")]
    q_embedded = await embed_chunks(query_chunk)
    if q_embedded:
        top_chunks = await retrieve_top_k(q_embedded[0]["embedding"], manual_id=manual_id, top_k=10)
    else:
        top_chunks = [{"text": e["text"], "page_start": e.get("page_start", 0), "page_end": e.get("page_end", 0), "source_file": e.get("source_file", "")} for e in embedded[:10]]

    interval_hints = [8, 120, 500, 1500, 3000, 6000] if classification.manufacturer == "KRONES" else [8, 240, 500, 1500]
    extracted_tasks = await extract_tasks_from_chunks(
        top_chunks,
        manufacturer=classification.manufacturer,
        model=classification.model,
        interval_hints=interval_hints,
    )
    log.info("[%s] Extracted %d tasks — writing to DB", manual_id, len(extracted_tasks))

    finalize_fn(
        json.dumps(extracted_tasks),
        classification.manufacturer,
        json.dumps(classification.detected_chapters),
    )
