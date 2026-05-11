from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile, status

from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.rag.pipeline import run_pipeline
from app.utils.audit import log_action
from app.utils.security import compute_bytes_hash, sanitize_filename

router = APIRouter(prefix="/api/manual", tags=["Manual Upload / RAG"])

_ALLOWED_TYPES = {"application/pdf", "application/octet-stream"}
_MAX_FILE_MB = 50


def _basic_pdf_safety_check(content: bytes) -> bool:
    """
    Basic PDF safety check (Architecture: 'All uploads virus scanned').
    Production: replace with Microsoft Defender for Cloud Storage webhook
    or ClamAV daemon integration. This stub blocks obvious non-PDF and
    embedded JavaScript patterns.
    """
    if not content.startswith(b"%PDF"):
        return False
    # Block PDFs with embedded JavaScript (common exploit vector)
    dangerous_patterns = [b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile"]
    for pattern in dangerous_patterns:
        if pattern in content[:8192]:
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
    """Background task wrapper — creates its own DB session."""
    from app.db.database import get_db_session

    async with get_db_session() as db:
        try:
            await run_pipeline(db, manual_id, pdf_path)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "Background pipeline task failed for %s: %s", manual_id, exc
            )
        finally:
            import shutil
            parent = pdf_path.parent
            try:
                shutil.rmtree(str(parent), ignore_errors=True)
            except Exception:
                pass
