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

    # Archive PDF permanently to Azure Blob Storage (keyed by manual_id prefix)
    blob_url: Optional[str] = None
    try:
        from app.config import get_settings as _gs
        _s = _gs()
        if _s.azure_storage_connection_string or _s.azure_storage_account_name:
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential as _DAC
            _conn = _s.azure_storage_connection_string
            if _conn:
                _bsc = BlobServiceClient.from_connection_string(_conn)
            else:
                _bsc = BlobServiceClient(
                    account_url=f"https://{_s.azure_storage_account_name}.blob.core.windows.net",
                    credential=_DAC(),
                )
            # Blob path: manuals/<manual_id>/<original_filename>
            # manual_id not yet known — use placeholder, updated after DB insert
            _blob_name_tmp = f"manuals/pending/{safe_name}"
            _cc = _bsc.get_container_client(_s.azure_storage_container_name)
            with open(tmp_path, "rb") as _f:
                _cc.upload_blob(name=_blob_name_tmp, data=_f, overwrite=True)
            blob_url = f"https://{_s.azure_storage_account_name or _bsc.account_name}.blob.core.windows.net/{_s.azure_storage_container_name}/{_blob_name_tmp}"
    except Exception as _be:
        import logging as _log
        _log.getLogger(__name__).warning("PDF Blob archive failed (non-fatal): %s", _be)

    # Create DB record
    upload_record = await crud.create_manual_upload(
        db,
        {
            "original_filename": safe_name,
            "machine_id": machine_id,
            "local_path": str(tmp_path),
            "blob_url": blob_url,
            "file_size_bytes": len(content),
            "status": "UPLOADED",
            "uploaded_by": user.email,
        },
    )

    # Re-key the blob under the real manual_id now that we have it
    if blob_url and upload_record.manual_id:
        try:
            _final_blob = f"manuals/{upload_record.manual_id}/{safe_name}"
            _cc.copy_blob(_cc.get_blob_client(_final_blob), blob_url)
            _cc.delete_blob(f"manuals/pending/{safe_name}")
            blob_url = blob_url.replace(f"manuals/pending/{safe_name}", _final_blob)
            await crud.update_manual_upload(db, upload_record.manual_id, {"blob_url": blob_url})
        except Exception:
            pass

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

    # Commit and CLOSE before the background task starts.
    # FastAPI's BackgroundTasks run before dependency-generator teardown, so the
    # ORM session stays alive (and holds the SQLite write-lock pool slot) for the
    # entire pipeline unless we explicitly close it here.
    await db.commit()
    await db.close()

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
        "PENDING_REVIEW": (100, "Processing complete — ready to generate documents!"),
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
    # Extract model from first task's part_number or detect from manufacturer string
    detected_model = None
    if upload.detected_manufacturer:
        parts = upload.detected_manufacturer.split(None, 1)
        if len(parts) > 1:
            detected_model = parts[1]
    return {
        "manual_id": upload.manual_id,
        "filename": upload.original_filename,
        "status": upload.status,
        "machine_id": upload.machine_id,
        "detected_manufacturer": upload.detected_manufacturer,
        "detected_model": detected_model,
        "detected_chapters": json.loads(upload.detected_chapters or "[]"),
        "extracted_task_count": len(tasks),
        "extracted_tasks": tasks if upload.status in ("PENDING_REVIEW", "APPROVED") else [],
        "error_message": upload.error_message,
        "uploaded_by": upload.uploaded_by,
        "approved_by": upload.approved_by,
        "created_at": upload.created_at.isoformat(),
    }


@router.delete("/uploads/{manual_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload(manual_id: str, user: CurrentUserDep, db: DBDep) -> None:
    """Delete a manual upload record."""
    user.require("manual:upload")
    deleted = await crud.delete_manual_upload(db, manual_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")


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


@router.post("/uploads/{manual_id}/reject", response_model=dict)
async def reject_extracted_tasks(
    manual_id: str,
    user: CurrentUserDep,
    db: DBDep,
    comment: str = Form(...),
    interval_hours: int = Form(0),
) -> dict:
    """
    Engineer rejects extracted tasks with a mandatory comment.
    Saves rejection to Azure SQL Approvals table.
    """
    user.require("manual:approve")

    if not comment or not comment.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rejection comment is required",
        )

    upload = await crud.get_manual_upload(db, manual_id)
    if not upload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    if upload.status not in ("PENDING_REVIEW", "APPROVED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload cannot be rejected (status: {upload.status})",
        )

    approval = await crud.save_manual_approval(
        db,
        {
            "manual_id": manual_id,
            "interval_hours": interval_hours,
            "status": "REJECTED",
            "comment": comment.strip(),
            "reviewed_by": user.email,
        },
    )

    await log_action(
        db,
        action="MANUAL_REJECTED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="manual_upload",
        resource_id=manual_id,
        details={"interval_hours": interval_hours, "comment": comment.strip()},
        ip_address=user.ip_address,
    )

    return {
        "manual_id": manual_id,
        "approval_id": approval.approval_id,
        "status": "REJECTED",
        "comment": comment.strip(),
        "reviewed_by": user.email,
    }


@router.get("/uploads/{manual_id}/citations", response_model=list[dict])
async def get_citations(
    manual_id: str,
    user: CurrentUserDep,
    db: DBDep,
    content_type: Optional[str] = None,
) -> list[dict]:
    """
    GET /api/manual/uploads/{manual_id}/citations
    Returns all citation records for a manual — enables clickable page links in the review UI.
    Optionally filter by content_type (warning, loto, ppe, procedure, etc.)
    """
    user.require("manual:upload")

    if content_type:
        citations = await crud.get_citations_by_content_type(db, manual_id, content_type)
    else:
        citations = await crud.get_citations_for_manual(db, manual_id)

    return [
        {
            "citation_id": c.citation_id,
            "manual_id": c.manual_id,
            "chunk_id": c.chunk_id,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "section": c.section,
            "content_type": c.content_type,
            "text_excerpt": c.text_excerpt,
            "manual_version": c.manual_version,
        }
        for c in citations
    ]


@router.get("/uploads/{manual_id}/generate-zip")
async def generate_zip_download(
    manual_id: str,
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = None,
):
    """
    GET /api/manual/uploads/{manual_id}/generate-zip?machine_id=...
    Streams a ZIP file containing one CON L3 XLSX per PM interval.
    No approval required — works directly from PENDING_REVIEW state.
    """
    from fastapi.responses import Response
    from app.core.document_generator import generate_con_l3_zip_bytes

    user.require("manual:upload")

    upload = await crud.get_manual_upload(db, manual_id)
    if not upload or upload.status not in ("PENDING_REVIEW", "APPROVED"):
        raise HTTPException(status_code=400, detail="Manual not ready for generation")

    raw_tasks = json.loads(upload.extracted_tasks or "[]")
    if not raw_tasks:
        raise HTTPException(status_code=404, detail="No extracted tasks found")

    # Resolve machine name: prefer selected machine from DB, fall back to detected manufacturer
    mid = machine_id or upload.machine_id
    machine_display_name = None
    effective_machine_id = mid or ""
    if mid:
        machine = await crud.get_machine(db, mid)
        if machine:
            machine_display_name = machine.name
            effective_machine_id = machine.machine_id
    if not machine_display_name:
        machine_display_name = (
            upload.detected_manufacturer
            or Path(upload.original_filename).stem.replace("_", " ").replace("-", " ")
            or "EQUIPMENT"
        )

    tasks = [
        {
            "task_no": t.get("task_no", (i + 1) * 10),
            "area": t.get("area", "GENERAL"),
            "action": t.get("action", "CHECK"),
            "description": t.get("description", ""),
            "interval_hours": int(t.get("interval_hours", 0) or 0),
        }
        for i, t in enumerate(raw_tasks)
    ]

    zip_bytes, zip_filename = generate_con_l3_zip_bytes(effective_machine_id, machine_display_name, tasks)

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@router.post("/uploads/{manual_id}/generate-xlsx", response_model=dict)
async def generate_xlsx_per_interval(
    manual_id: str,
    user: CurrentUserDep,
    db: DBDep,
) -> dict:
    """
    POST /api/manual/uploads/{manual_id}/generate-xlsx
    Generate one XLSX file per PM interval from a processed manual's extracted tasks.
    Returns download links for each interval file.
    """
    user.require("manual:upload")

    from app.core.pm_generation import generate_pm_xlsx_per_interval as _gen

    docs = await _gen(db, user, manual_id)
    if not docs:
        upload = await crud.get_manual_upload(db, manual_id)
        if not upload:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
        if upload.status not in ("PENDING_REVIEW", "APPROVED"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Manual is not ready (status: {upload.status}). Wait for pipeline to complete.",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No extracted tasks found in this manual.",
        )

    return {
        "manual_id": manual_id,
        "file_count": len(docs),
        "total_tasks": sum(d["task_count"] for d in docs),
        "files": docs,
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
            conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
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

    def _raw_finalize(extracted_tasks_json: str, manufacturer: str, chapters_json: str, inferred_machine_id: str = "") -> None:
        """Write final pipeline results via raw sqlite3."""
        try:
            conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """UPDATE manual_uploads
                   SET status='PENDING_REVIEW',
                       extracted_tasks=?,
                       detected_manufacturer=?,
                       detected_chapters=?,
                       machine_id=COALESCE(NULLIF(machine_id,''), NULLIF(?,''), machine_id),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE manual_id=?""",
                (extracted_tasks_json, manufacturer, chapters_json, inferred_machine_id, manual_id),
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


def _infer_machine_id(manufacturer: str, model: Optional[str]) -> str:
    """Map classification result to a known machine_id for CON L3 ZIP generation."""
    mfr = (manufacturer or "").upper()
    mod = (model or "").upper()
    if "EISBAR" in mfr or "DEHUMID" in mfr:
        return "DEHUMIDIFIER-L3"
    if "TETRA" in mfr:
        return "TETRAPAK-ASEPTIC-L3"
    if any(k in mfr for k in ("KRONES", "VARIOPAC", "CONTIFORM", "SHRINK")):
        if "SHRINK" in mod:
            return "SHRINK-TUNNEL-L3"
        if "VARIOPAC" in mod:
            return "VARIOPAC-PRO-L3"
        if "CONTIFORM" in mod:
            return "CONTIFORM-C3-L3"
        return "VARIOPAC-PRO-L3"  # KRONES default
    if "SIG" in mfr or "COMBIBLOC" in mfr:
        return ""
    return ""


async def _run_pipeline_direct(manual_id: str, pdf_path: Path, update_fn, finalize_fn) -> None:
    """
    Runs the full RAG pipeline calling update_fn(status) for progress updates
    and finalize_fn(tasks_json, manufacturer, chapters_json, machine_id) when complete.
    Avoids any ORM session for status writes — only uses raw sqlite3 via callbacks.
    """
    import json
    import logging
    from app.rag.chunker import TextChunk, smart_chunk_pdf, extract_text_from_pdf
    from app.rag.classifier import classify_manual
    from app.rag.embedder import embed_chunks
    from app.rag.extractor import extract_tasks_from_chunks
    from app.rag.retriever import index_chunks, retrieve_top_k
    from app.config import get_settings

    log = logging.getLogger(__name__)
    settings = get_settings()

    update_fn("CLASSIFYING")
    log.info("[%s] Extracting text + chunking in parallel (both non-blocking)", manual_id)

    # Both text extraction and chunking run in thread pool simultaneously
    # extract_text_from_pdf is capped at 60 pages so it finishes in ~10s
    chunk_task = asyncio.ensure_future(
        asyncio.to_thread(
            smart_chunk_pdf,
            pdf_path,
            pdf_path.name,
            settings.rag_max_section_words,
            settings.rag_min_section_words,
            manual_id,
            "",
        )
    )
    text_task = asyncio.ensure_future(
        asyncio.to_thread(extract_text_from_pdf, pdf_path)
    )

    # Wait for text extraction first (fast — 60-page cap), then classify
    full_text, _offsets = await text_task
    sample_text = full_text[:15000]

    # Classify finishes in ~5s; wrap with timeout so a crashed Ollama can't hang forever
    try:
        classification = await asyncio.wait_for(
            classify_manual(pdf_path, sample_text), timeout=30
        )
    except asyncio.TimeoutError:
        log.warning("[%s] Classification timed out — using keyword fallback", manual_id)
        from app.rag.classifier import _keyword_classify
        classification = _keyword_classify(sample_text)

    log.info("[%s] Classified: %s %s", manual_id, classification.manufacturer, classification.model)

    inferred_machine_id = _infer_machine_id(classification.manufacturer, classification.model)
    log.info("[%s] Inferred machine_id: %s", manual_id, inferred_machine_id or "none")

    # Update to CHUNKING — user sees the pipeline advance while the thread finishes
    update_fn("CHUNKING")
    chunks = await chunk_task

    type_summary = ', '.join(
        f'{t}={sum(1 for c in chunks if c.chunk_type == t)}'
        for t in dict.fromkeys(c.chunk_type for c in chunks)
    )
    log.info("[%s] Smart chunking: %d chunks (%s)", manual_id, len(chunks), type_summary)

    update_fn("EMBEDDING")
    from app.rag.pipeline import (
        _retrieve_8_pass_local, _select_top_chunks_by_type,
        _guess_intervals, _extract_tasks_from_pdf_tables, _validate_task_citations,
        _extract_pmrspl_direct,
    )

    # Tetra Pak PMRSPL direct path — bypasses AI; structured table has all data
    is_tetra = any(k in (classification.manufacturer or "").upper() for k in ("TETRA", "TEM"))
    if is_tetra:
        log.info("[%s] Tetra Pak detected — running PMRSPL direct extractor", manual_id)
        extracted_tasks = await asyncio.to_thread(_extract_pmrspl_direct, pdf_path)
        if extracted_tasks:
            log.info("[%s] PMRSPL direct: %d tasks", manual_id, len(extracted_tasks))
            for i, t in enumerate(extracted_tasks, 1):
                t["task_no"] = i * 10
            finalize_fn(
                json.dumps(extracted_tasks),
                classification.manufacturer,
                json.dumps(classification.detected_chapters or []),
                inferred_machine_id,
            )
            return
        log.warning("[%s] PMRSPL direct returned 0 tasks — falling through to AI path", manual_id)

    # Filter < 8h to exclude false positives (chapter numbers, display values, figure refs)
    chunk_intervals = list({c.interval_hint for c in chunks if c.interval_hint and c.interval_hint >= 8})
    interval_hints = list(set(chunk_intervals) | set(_guess_intervals(classification.manufacturer)))

    if settings.ai_provider != "watsonx":
        # Local dev fast path: 8-pass content_type filtering — no embedding needed
        top_chunks = _retrieve_8_pass_local(chunks, interval_hints)
        if not top_chunks:
            top_chunks = _select_top_chunks_by_type(chunks, settings.rag_top_k)
        embedded = []
        log.info("[%s] Local dev 8-pass: selected %d chunks", manual_id, len(top_chunks))
    else:
        # Production: full embed + index + Azure AI Search hybrid retrieval
        priority = [c for c in chunks if c.chunk_type in ("table_row", "checkbox")]
        others   = [c for c in chunks if c.chunk_type not in ("table_row", "checkbox")]
        embed_subset = (priority + others)[:200]
        embedded = await embed_chunks(embed_subset)
        log.info("[%s] Embedded %d/%d chunks", manual_id, len(embedded), len(chunks))
        if embedded:
            await index_chunks(embedded, manual_id)
        proxy = next(
            (e for e in embedded if e.get("chunk_type") in ("table_row", "checkbox")),
            embedded[0] if embedded else None,
        )
        if proxy:
            top_chunks = await retrieve_top_k(proxy["embedding"], manual_id=manual_id, top_k=10)
        else:
            top_chunks = [{"text": e["text"], "page_start": e.get("page_start", 0),
                           "page_end": e.get("page_end", 0), "source_file": e.get("source_file", "")}
                          for e in embedded[:10]]

    extracted_tasks = await extract_tasks_from_chunks(
        top_chunks,
        manufacturer=classification.manufacturer,
        model=classification.model,
        interval_hints=interval_hints,
    )

    if not extracted_tasks:
        log.warning("[%s] AI extraction returned 0 tasks — trying table-based fallback", manual_id)
        extracted_tasks = _extract_tasks_from_pdf_tables(pdf_path)
        if extracted_tasks:
            log.info("[%s] Table fallback extracted %d tasks", manual_id, len(extracted_tasks))

    # Save citations — one record per retrieved chunk so UI shows page/section links.
    # Accept page_start=0: pdfplumber sometimes cannot determine page number but the
    # text excerpt is still valid evidence the content exists in the document.
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
            "manufacturer": classification.manufacturer or "",
            "machine_model": classification.model or "",
        }
        for c in top_chunks
        if c.get("text", "").strip()  # include any chunk that has content
    ]

    # When AI returned 0 tasks and fallback ran, build citations from PDF pages
    # that contain PM content — gives engineers page references even without AI chunks
    if not citation_records and extracted_tasks:
        import pdfplumber as _plumber
        _PM_KW = ("maintenance", "inspect", "replace", "check", "lubricate",
                  "interval", "hours", "service", "clean", "grease")
        try:
            with _plumber.open(str(pdf_path)) as _pdf:
                for _page in _pdf.pages:
                    _txt = _page.extract_text() or ""
                    if any(kw in _txt.lower() for kw in _PM_KW):
                        citation_records.append({
                            "manual_id": manual_id,
                            "chunk_id": f"fallback_p{_page.page_number}",
                            "page_start": _page.page_number,
                            "page_end": _page.page_number,
                            "section": "",
                            "content_type": "procedure",
                            "text_excerpt": _txt[:500],
                            "manual_version": "",
                            "manufacturer": classification.manufacturer or "",
                            "machine_model": classification.model or "",
                        })
                        if len(citation_records) >= 25:
                            break
        except Exception as _pe:
            log.warning("[%s] Fallback citation scan failed: %s", manual_id, _pe)

    if citation_records:
        import sqlite3 as _sqlite3
        try:
            conn = _sqlite3.connect(db_path, timeout=30, isolation_level=None)
            # Ensure new columns exist (safe no-op if already present)
            for _col, _typ in (("manufacturer", "TEXT"), ("machine_model", "TEXT")):
                try:
                    conn.execute(f"ALTER TABLE citations ADD COLUMN {_col} {_typ}")
                except Exception:
                    pass
            conn.executemany(
                "INSERT OR IGNORE INTO citations "
                "(citation_id, manual_id, chunk_id, page_start, page_end, section, "
                "content_type, text_excerpt, manual_version, manufacturer, machine_model) "
                "VALUES (lower(hex(randomblob(16))),?,?,?,?,?,?,?,?,?,?)",
                [
                    (r["manual_id"], r["chunk_id"], r["page_start"], r["page_end"],
                     r["section"], r["content_type"], r["text_excerpt"], r["manual_version"],
                     r.get("manufacturer", ""), r.get("machine_model", ""))
                    for r in citation_records
                ],
            )
            conn.close()
            log.info("[%s] Saved %d citations (raw sqlite)", manual_id, len(citation_records))
        except Exception as ce:
            log.warning("[%s] Citation save failed: %s", manual_id, ce)

    extracted_tasks = _validate_task_citations(extracted_tasks, citation_records)
    unverified = sum(1 for t in extracted_tasks if t.get("validation_status") == "UNVERIFIED")
    if unverified:
        log.warning("[%s] Validation: %d/%d tasks UNVERIFIED", manual_id, unverified, len(extracted_tasks))
    else:
        log.info("[%s] Validation passed: all %d tasks have citations", manual_id, len(extracted_tasks))

    log.info("[%s] Extracted %d tasks — writing to DB", manual_id, len(extracted_tasks))

    finalize_fn(
        json.dumps(extracted_tasks),
        classification.manufacturer,
        json.dumps(classification.detected_chapters),
        inferred_machine_id,
    )
