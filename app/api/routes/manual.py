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
    skipped_count = 0
    for t in tasks:
        if not t.get("interval_hours"):
            continue
        try:
            async with db.begin_nested():
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
                        "source_chapter": f"RAG Extract — {upload.original_filename}"[:64],
                        "source_section": "AI Extracted",
                    },
                )
            added_count += 1
        except Exception:
            skipped_count += 1

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
        "tasks_skipped_duplicates": skipped_count,
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
            "manufacturer": getattr(c, "manufacturer", None),
            "machine_model": getattr(c, "machine_model", None),
            "interval_hours": getattr(c, "interval_hours", 0) or 0,
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

    # Save ZIP to Azure Blob Storage so it can be retrieved later without regenerating.
    try:
        from app.config import get_settings as _gs2
        _s2 = _gs2()
        if _s2.azure_storage_connection_string or _s2.azure_storage_account_name:
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential as _DAC2
            _conn2 = _s2.azure_storage_connection_string
            if _conn2:
                _bsc2 = BlobServiceClient.from_connection_string(_conn2)
            else:
                _bsc2 = BlobServiceClient(
                    account_url=f"https://{_s2.azure_storage_account_name}.blob.core.windows.net",
                    credential=_DAC2(),
                )
            _blob_name = f"exports/{manual_id}/{zip_filename}"
            _cc2 = _bsc2.get_container_client(_s2.azure_storage_container_name)
            _cc2.upload_blob(name=_blob_name, data=zip_bytes, overwrite=True)
            import logging as _log2
            _log2.getLogger(__name__).info("ZIP saved to Blob: %s", _blob_name)
    except Exception as _blob_err:
        import logging as _log3
        _log3.getLogger(__name__).warning("ZIP Blob save failed (non-fatal): %s", _blob_err)

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
    Uses a sync SQLAlchemy engine for status updates — works with both SQLite
    (local dev, DATABASE_URL unset) and Azure SQL (production, DATABASE_URL=mssql+pyodbc://...).
    Raw sqlite3 cannot handle mssql URLs; using SQLAlchemy avoids that entirely.
    The original request session is already committed and closed before this runs,
    so there is no write-lock contention on either database.
    """
    import asyncio
    import logging
    import shutil
    from pathlib import Path as _Path

    log = logging.getLogger(__name__)

    from sqlalchemy import text as _sql_text
    from app.db.database import AsyncSessionLocal as _AsyncSessionLocal

    async def _raw_update(status: str, error: str = "") -> None:
        """Write status update via async ORM session — same engine the app uses, no blocking."""
        try:
            async with _AsyncSessionLocal() as _session:
                if error:
                    await _session.execute(
                        _sql_text("UPDATE manual_uploads SET status=:s, error_message=:e, updated_at=CURRENT_TIMESTAMP WHERE manual_id=:mid"),
                        {"s": status, "e": error[:2000], "mid": manual_id},
                    )
                else:
                    await _session.execute(
                        _sql_text("UPDATE manual_uploads SET status=:s, updated_at=CURRENT_TIMESTAMP WHERE manual_id=:mid"),
                        {"s": status, "mid": manual_id},
                    )
                await _session.commit()
        except Exception as e:
            log.warning("raw_update failed (%s): %s", status, e)

    async def _raw_finalize(extracted_tasks_json: str, manufacturer: str, chapters_json: str, inferred_machine_id: str = "") -> None:
        """Write final pipeline results via async ORM session."""
        try:
            async with _AsyncSessionLocal() as _session:
                await _session.execute(
                    _sql_text(
                        "UPDATE manual_uploads"
                        " SET status='PENDING_REVIEW',"
                        "     extracted_tasks=:tasks,"
                        "     detected_manufacturer=:mfr,"
                        "     detected_chapters=:chapters,"
                        "     machine_id=COALESCE(NULLIF(machine_id,''), NULLIF(:mid,''), machine_id),"
                        "     updated_at=CURRENT_TIMESTAMP"
                        " WHERE manual_id=:manual_id"
                    ),
                    {
                        "tasks": extracted_tasks_json,
                        "mfr": manufacturer,
                        "chapters": chapters_json,
                        "mid": inferred_machine_id,
                        "manual_id": manual_id,
                    },
                )
                await _session.commit()
        except Exception as e:
            log.warning("raw_finalize failed: %s", e)

    try:
        await _run_pipeline_direct(manual_id, pdf_path, _raw_update, _raw_finalize)
    except Exception as exc:
        log.error("Background pipeline task failed for %s: %s", manual_id, exc)
        await _raw_update("FAILED", str(exc))
    finally:
        parent = pdf_path.parent
        try:
            shutil.rmtree(str(parent), ignore_errors=True)
        except Exception:
            pass


def _infer_machine_id(manufacturer: str, model: Optional[str], filename: str = "") -> str:
    """Map classification result to a known machine_id for CON L3 ZIP generation."""
    mfr = (manufacturer or "").upper()
    mod = (model or "").upper()
    fname = (filename or "").upper()

    if "EISBAR" in mfr or "DEHUMID" in mfr or "DEHUMID" in fname:
        return "DEHUMIDIFIER-L3"
    if "TETRA" in mfr or any(k in fname for k in ("TETRA", "TEM-", "TETRAPAK")):
        return "TETRAPAK-ASEPTIC-L3"
    if any(k in mfr or k in fname for k in ("HUSKY", "HYPET", "HPP5E", "HPET")):
        return ""  # HyPET uses Excel, not SQL — handled separately
    if any(k in mfr or k in fname for k in ("KRONES", "VARIOPAC", "CONTIFORM", "SHRINK")):
        if "SHRINK" in mod or "SHRINK" in fname:
            return "SHRINK-TUNNEL-L3"
        if "VARIOPAC" in mod or "VARIOPAC" in fname:
            return "VARIOPAC-PRO-L3"
        if "CONTIFORM" in mod or "CONTIFORM" in fname:
            return "CONTIFORM-C3-L3"
        return "VARIOPAC-PRO-L3"  # KRONES default
    if "SIG" in mfr or "COMBIBLOC" in mfr:
        return ""
    return ""


async def _run_pipeline_direct(manual_id: str, pdf_path: Path, update_fn, finalize_fn) -> None:
    """
    Runs the full RAG pipeline calling await update_fn(status) for progress updates
    and await finalize_fn(tasks_json, manufacturer, chapters_json, machine_id) when done.
    All DB writes go through async ORM sessions — no sync blocking on the event loop.
    """
    import json
    import logging
    from pathlib import Path as _Path
    from sqlalchemy import text as _dtext
    from app.db.database import AsyncSessionLocal as _ASL
    from app.rag.chunker import TextChunk, smart_chunk_pdf, extract_text_from_pdf
    from app.rag.classifier import classify_manual, extract_manual_version
    from app.rag.embedder import embed_chunks
    from app.rag.extractor import extract_tasks_from_chunks
    from app.rag.retriever import index_chunks
    from app.config import get_settings

    log = logging.getLogger(__name__)
    settings = get_settings()

    await update_fn("CLASSIFYING")

    _fname_upper_m = pdf_path.name.upper()
    _file_size_mb = pdf_path.stat().st_size / (1024 * 1024)
    log.info("[%s] file=%.1fMB — extracting text (150-page cap)", manual_id, _file_size_mb)
    try:
        full_text, _offsets = await asyncio.wait_for(
            asyncio.to_thread(extract_text_from_pdf, pdf_path), timeout=480  # 8 min max — then SQL fallback
        )
    except asyncio.TimeoutError:
        log.warning("[%s] Text extraction timed out (8 min) — falling back to SQL/Excel library", manual_id)
        full_text, _offsets = "", {}
    except Exception as _te:
        log.warning("[%s] Text extraction failed: %s — using empty text", manual_id, _te)
        full_text, _offsets = "", {}
    sample_text = full_text[:15000]

    # Classify finishes in ~5s; wrap with timeout to prevent hanging on slow network
    try:
        classification = await asyncio.wait_for(
            classify_manual(pdf_path, sample_text), timeout=30
        )
    except asyncio.TimeoutError:
        log.warning("[%s] Classification timed out — using keyword fallback", manual_id)
        from app.rag.classifier import _keyword_classify
        classification = _keyword_classify(sample_text)

    log.info("[%s] Classified: %s %s", manual_id, classification.manufacturer, classification.model)

    # Extract manual version from cover page text (Rev. A, Issue 3, Version 1.2, etc.)
    manual_version = extract_manual_version(sample_text)
    if manual_version:
        log.info("[%s] Detected manual version: %s", manual_id, manual_version)

    inferred_machine_id = _infer_machine_id(classification.manufacturer, classification.model, _fname_upper_m)
    log.info("[%s] Inferred machine_id: %s", manual_id, inferred_machine_id or "none")

    # Detect fast-path machines by filename + classifier — determines timeout strategy.
    # _fname_upper_m already set above. is_tetra also checks filename for "TeM-" prefix
    # (Tetra Pak's document numbering scheme) so it works even when IBM 403 blocks classification.
    is_tetra = (
        any(k in (classification.manufacturer or "").upper() for k in ("TETRA", "TEM"))
        or any(k in _fname_upper_m for k in ("TETRA", "TEM-", "TETRAPAK"))
    )
    is_hypet = (
        any(k in _fname_upper_m for k in ("HYPET", "HPET", "HPP5E", "HYPET5"))
        or any(k in (classification.manufacturer or "").upper() for k in ("HUSKY", "HYPET"))
    )
    log.info("[%s] is_tetra=%s is_hypet=%s manufacturer=%r file=%.1fMB", manual_id, is_tetra, is_hypet, classification.manufacturer, _file_size_mb)

    # Start chunk_task now that text extraction is complete (no more GIL contention)
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

    # Update to CHUNKING — user sees the pipeline advance while the thread finishes
    await update_fn("CHUNKING")
    _chunking_used_fallback = False
    # Tetra Pak PDFs (800+ pages) saturate the GIL in smart_chunk_pdf; PMRSPL
    # handles their task extraction anyway, so fall back to sliding window in 20s.
    # All machines get up to 8 min for chunking (covers OCR on scanned PDFs).
    # SQL/Excel library always supplements so even a timeout still yields good results.
    _chunk_timeout = 480
    try:
        # Shield so a timeout doesn't block the event loop waiting for the thread.
        # The underlying thread cannot be preempted; shield lets us fall back
        # immediately while the thread finishes quietly in the background.
        chunks = await asyncio.wait_for(asyncio.shield(chunk_task), timeout=_chunk_timeout)
    except asyncio.TimeoutError:
        _chunking_used_fallback = True
        log.warning("[%s] Smart chunking timed out (%ds) — sliding window fallback", manual_id, _chunk_timeout)
        from app.rag.chunker import chunk_text as _chunk_text
        chunks = _chunk_text(full_text, str(pdf_path.name), page_offsets=_offsets)
        log.info("[%s] Sliding window fallback: %d chunks", manual_id, len(chunks))
    except Exception as _chunk_exc:
        _chunking_used_fallback = True
        log.warning("[%s] Smart chunking failed (%s) — sliding window fallback", manual_id, _chunk_exc)
        from app.rag.chunker import chunk_text as _chunk_text
        chunks = _chunk_text(full_text, str(pdf_path.name), page_offsets=_offsets)
        log.info("[%s] Sliding window fallback: %d chunks", manual_id, len(chunks))

    type_summary = ', '.join(
        f'{t}={sum(1 for c in chunks if c.chunk_type == t)}'
        for t in dict.fromkeys(c.chunk_type for c in chunks)
    )
    log.info("[%s] Smart chunking: %d chunks (%s)", manual_id, len(chunks), type_summary)

    await update_fn("EMBEDDING")
    from app.rag.pipeline import (
        _guess_intervals, _extract_tasks_from_pdf_tables, _validate_task_citations,
        _extract_pmrspl_direct, _assign_task_page_citations, _extract_hypet_direct,
    )

    log.info("[%s] is_tetra=%s is_hypet=%s manufacturer=%r chunks=%d — using full AI pipeline", manual_id, is_tetra, is_hypet, classification.manufacturer, len(chunks))

    # Filter < 8h to exclude false positives (chapter numbers, display values, figure refs)
    chunk_intervals = list({c.interval_hint for c in chunks if c.interval_hint and c.interval_hint >= 8})
    interval_hints = list(set(chunk_intervals) | set(_guess_intervals(classification.manufacturer)))

    # Balance the embed subset so section/paragraph chunks always get slots.
    # Without this, a PDF with 200 table rows fills all 200 embed slots, leaving
    # no section chunks for the extraction pool's section[:15] bucket.
    _es_tables   = [c for c in chunks if c.chunk_type == "table_row"]
    _es_cboxes   = [c for c in chunks if c.chunk_type == "checkbox"]
    _es_sections = [c for c in chunks if c.chunk_type in ("section", "paragraph")]
    _es_other    = [c for c in chunks if c.chunk_type not in
                    ("table_row", "checkbox", "section", "paragraph")]
    embed_subset = (_es_tables[:80] + _es_cboxes[:60] + _es_sections[:50] + _es_other[:10])[:200]
    log.info("[%s] Calling embed_chunks with %d chunks (tables=%d cboxes=%d sections=%d other=%d)",
             manual_id, len(embed_subset), len(_es_tables[:80]), len(_es_cboxes[:60]),
             len(_es_sections[:50]), len(_es_other[:10]))
    embedded = await embed_chunks(embed_subset)
    log.info("[%s] Embedded %d/%d chunks", manual_id, len(embedded), len(chunks))
    _embedding_failed = len(embedded) == 0 and len(embed_subset) > 0
    if _embedding_failed:
        log.warning("[%s] Embedding returned 0 — will attempt extraction from raw text chunks", manual_id)
    if embedded:
        await index_chunks(embedded, manual_id)

    # Build extraction chunk list — use ALL embedded chunks, prioritising
    # table_row/checkbox types which carry interval data most reliably.
    # Cap at 60 chunks (15 batches × 4 chunks) to stay within timeout budget.
    # Skip similarity retrieval for extraction — RAG retrieval is for chat;
    # extraction needs every section of the manual, not just top-similar ones.
    if embedded:
        # Balanced pool: table_row/checkbox carry structured interval data;
        # section/paragraph carry the actual task prose for manuals like Krones
        # that describe tasks in bullet paragraphs, not structured tables.
        # Without section slots, 47 useless navigation header rows crowd out
        # the actual maintenance text.
        _emb_tables   = [e for e in embedded if e.get("chunk_type") == "table_row"]
        _emb_cboxes   = [e for e in embedded if e.get("chunk_type") == "checkbox"]
        _emb_sections = [e for e in embedded if e.get("chunk_type") in ("section", "paragraph")]
        _emb_other    = [e for e in embedded if e.get("chunk_type") not in
                         ("table_row", "checkbox", "section", "paragraph")]
        # Up to 25 tables + 15 checkboxes + 15 section text + 5 other = 60 max
        _extraction_pool = (_emb_tables[:25] + _emb_cboxes[:15] +
                            _emb_sections[:15] + _emb_other[:5])[:60]
        top_chunks = [
            {"text": e["text"], "page_start": e.get("page_start", 0),
             "page_end": e.get("page_end", 0), "source_file": e.get("source_file", "")}
            for e in _extraction_pool
        ]
    else:
        # Embedding failed — fall back to raw TextChunk objects so AI still runs
        _raw_tables   = [c for c in embed_subset if getattr(c, "chunk_type", "") == "table_row"]
        _raw_cboxes   = [c for c in embed_subset if getattr(c, "chunk_type", "") == "checkbox"]
        _raw_sections = [c for c in embed_subset if getattr(c, "chunk_type", "") in ("section", "paragraph")]
        _raw_other    = [c for c in embed_subset if getattr(c, "chunk_type", "") not in
                         ("table_row", "checkbox", "section", "paragraph")]
        _raw_pool     = (_raw_tables[:25] + _raw_cboxes[:15] + _raw_sections[:15] + _raw_other[:5])[:60]
        top_chunks = [
            {"text": c.text, "page_start": c.page_start,
             "page_end": c.page_end, "source_file": str(getattr(c, "source_file", "") or "")}
            for c in _raw_pool
        ]

    log.info("[%s] Sending %d chunks to AI extraction (embedding_ok=%s)",
             manual_id, len(top_chunks), not _embedding_failed)

    extracted_tasks = await extract_tasks_from_chunks(
        top_chunks,
        manufacturer=classification.manufacturer,
        model=classification.model,
        interval_hints=interval_hints,
    )

    # Table fallback: run whenever AI returned 0, regardless of chunking fallback
    if not extracted_tasks:
        log.warning("[%s] AI returned 0 tasks — trying table-based fallback", manual_id)
        _table_fut = asyncio.ensure_future(asyncio.to_thread(_extract_tasks_from_pdf_tables, pdf_path))
        try:
            extracted_tasks = await asyncio.wait_for(asyncio.shield(_table_fut), timeout=120)
        except asyncio.TimeoutError:
            log.warning("[%s] Table-based fallback timed out (120s) — skipping", manual_id)
            extracted_tasks = []
        if extracted_tasks:
            log.info("[%s] Table fallback extracted %d tasks", manual_id, len(extracted_tasks))

    # HyPET reference Excel fallback: AI on a scanned HyPET PDF yields very few tasks.
    # Load the manager-validated reference schedule and merge — AI tasks at intervals
    # not covered by the Excel are kept; the Excel provides the complete base set.
    if is_hypet and len(extracted_tasks) < 10:
        log.info("[%s] HyPET detected with <10 AI tasks — loading reference Excel", manual_id)
        try:
            _hypet_tasks = await asyncio.to_thread(_extract_hypet_direct)
            if _hypet_tasks:
                if not extracted_tasks:
                    extracted_tasks = _hypet_tasks
                else:
                    _hypet_intervals = {t["interval_hours"] for t in _hypet_tasks}
                    _ai_only = [t for t in extracted_tasks if t.get("interval_hours") not in _hypet_intervals]
                    extracted_tasks = _hypet_tasks + _ai_only
                log.info("[%s] HyPET reference: %d tasks total", manual_id, len(extracted_tasks))
        except Exception as _he:
            log.warning("[%s] HyPET reference load failed: %s", manual_id, _he)

    # Mark AI-extracted tasks with source so UI can distinguish them
    for _t in extracted_tasks:
        _t.setdefault("_source", "ai_extracted")

    # PM Library: always load and supplement AI results with intervals AI didn't cover.
    # Pure fallback if AI returned 0; supplement otherwise (adds 8hr daily checks,
    # 2000hr overhaul tasks, etc. that rarely appear as checkbox items in the PDF text).
    _lib_machine_id = inferred_machine_id
    if _lib_machine_id:
        try:
            async with _ASL() as _db2:
                _res2 = await _db2.execute(
                    _dtext(
                        "SELECT task_no, area, action, description, machine_state,"
                        " safety_flag, part_number, interval_hours"
                        " FROM tasks WHERE machine_id=:mid ORDER BY interval_hours, task_no"
                    ),
                    {"mid": _lib_machine_id},
                )
                _lib_rows = _res2.fetchall()
            if _lib_rows:
                _lib_tasks = [
                    {
                        "task_no": r[0], "area": r[1] or "GENERAL",
                        "action": r[2] or "CHECK", "description": r[3] or "",
                        "machine_state": r[4] or "STOPPED", "safety_flag": bool(r[5]),
                        "part_number": r[6], "interval_hours": r[7] or 500,
                        "_source": "pm_library",
                    }
                    for r in _lib_rows
                ]
                if not extracted_tasks:
                    # Pure fallback: AI found nothing at all
                    extracted_tasks = _lib_tasks
                    log.info("[%s] PM Library fallback (pure): %d tasks for %s",
                             manual_id, len(extracted_tasks), _lib_machine_id)
                else:
                    # Always include ALL PM Library tasks PLUS any AI tasks at
                    # intervals the PM Library doesn't have (e.g. 42000hr, 45000hr).
                    # Never drop PM Library tasks just because AI found a few at
                    # the same interval — the library has the complete set.
                    _lib_intervals = {t["interval_hours"] for t in _lib_tasks}
                    _ai_only = [t for t in extracted_tasks if t.get("interval_hours") not in _lib_intervals]
                    extracted_tasks = _lib_tasks + _ai_only
                    log.info(
                        "[%s] PM Library base (%d) + AI-only intervals (%d) → %d total",
                        manual_id, len(_lib_tasks), len(_ai_only), len(extracted_tasks),
                    )
        except Exception as _le:
            log.warning("[%s] PM Library load failed: %s", manual_id, _le)

    # Renumber task_no sequentially within each interval so there are no duplicates
    # after merging AI tasks with PM Library supplement tasks.
    _interval_counters: dict[int, int] = {}
    for _t in extracted_tasks:
        _iv = int(_t.get("interval_hours") or 500)
        _interval_counters[_iv] = _interval_counters.get(_iv, 0) + 1
        _t["task_no"] = _iv * 10 + _interval_counters[_iv]

    # Per-task citations: each extracted task gets one citation pointing to its
    # best-matching source chunk (scored by interval_hours match + keyword overlap).
    # page_start/page_end are also written back onto each task dict so the review
    # UI can show the source page in the task table.
    citation_records = _assign_task_page_citations(
        extracted_tasks,
        top_chunks,
        manual_id,
        classification.manufacturer or "",
        classification.model or "",
        manual_version or "",
    )

    # Fallback: when AI returned 0 tasks, scan PDF pages for PM content so engineers
    # at least have page evidence that the document contains PM information.
    if not citation_records:
        import pdfplumber as _plumber
        _PM_KW = ("maintenance", "inspect", "replace", "check", "lubricate",
                  "interval", "hours", "service", "clean", "grease")
        def _scan_citations_sync():
            records = []
            with _plumber.open(str(pdf_path)) as _pdf:
                for _page in _pdf.pages[:150]:
                    _txt = _page.extract_text() or ""
                    if any(kw in _txt.lower() for kw in _PM_KW):
                        records.append({
                            "manual_id": manual_id,
                            "chunk_id": f"fallback_p{_page.page_number}",
                            "page_start": _page.page_number,
                            "page_end": _page.page_number,
                            "section": "",
                            "content_type": "procedure",
                            "text_excerpt": _txt[:500],
                            "manual_version": manual_version or "",
                            "manufacturer": classification.manufacturer or "",
                            "machine_model": classification.model or "",
                        })
                        if len(records) >= 25:
                            break
            return records
        try:
            citation_records = await asyncio.wait_for(
                asyncio.to_thread(_scan_citations_sync),
                timeout=60,
            )
        except asyncio.TimeoutError:
            log.warning("[%s] Fallback citation scan timed out (60s) — skipping", manual_id)
        except Exception as _pe:
            log.warning("[%s] Fallback citation scan failed: %s", manual_id, _pe)

    if citation_records:
        import uuid as _uuid2
        try:
            async with _ASL() as _cdb2:
                for r in citation_records:
                    try:
                        await _cdb2.execute(
                            _dtext(
                                "INSERT INTO citations"
                                " (citation_id, manual_id, chunk_id, page_start, page_end, section,"
                                "  content_type, text_excerpt, manual_version, manufacturer, machine_model, interval_hours)"
                                " VALUES (:cid,:mid,:ck,:ps,:pe,:sec,:ct,:tx,:mv,:mfr,:mm,:ih)"
                            ),
                            {
                                "cid": _uuid2.uuid4().hex,
                                "mid": r["manual_id"], "ck": r["chunk_id"],
                                "ps": r["page_start"], "pe": r["page_end"],
                                "sec": r["section"], "ct": r["content_type"],
                                "tx": r["text_excerpt"], "mv": r["manual_version"],
                                "mfr": r.get("manufacturer", ""),
                                "mm": r.get("machine_model", ""),
                                "ih": r.get("interval_hours", 0),
                            },
                        )
                    except Exception:
                        pass
                await _cdb2.commit()
            log.info("[%s] Saved %d citations", manual_id, len(citation_records))
        except Exception as ce:
            log.warning("[%s] Citation save failed: %s", manual_id, ce)

    extracted_tasks = _validate_task_citations(extracted_tasks, citation_records)
    unverified = sum(1 for t in extracted_tasks if t.get("validation_status") == "UNVERIFIED")
    if unverified:
        log.warning("[%s] Validation: %d/%d tasks UNVERIFIED", manual_id, unverified, len(extracted_tasks))
    else:
        log.info("[%s] Validation passed: all %d tasks have citations", manual_id, len(extracted_tasks))

    log.info("[%s] Extracted %d tasks — writing to DB", manual_id, len(extracted_tasks))

    await finalize_fn(
        json.dumps(extracted_tasks),
        classification.manufacturer,
        json.dumps(classification.detected_chapters),
        inferred_machine_id,
    )
