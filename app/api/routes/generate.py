from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from app.config import get_settings
from app.core.document_generator import PMDocument, generate_docx, generate_pdf, generate_xlsx
from app.core.pm_library import extract_parts_from_tasks, get_interval_label
from app.core.storage_module import upload_file
from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.schemas.generate import GenerateRequest, GenerateResponse
from app.utils.audit import log_action
from app.utils.security import compute_file_hash, sanitize_filename

router = APIRouter(prefix="/api/generate", tags=["Generate PM"])
settings = get_settings()


@router.post("", response_model=GenerateResponse, status_code=status.HTTP_201_CREATED)
async def generate_pm(
    req: GenerateRequest,
    user: CurrentUserDep,
    db: DBDep,
) -> GenerateResponse:
    """
    POST /api/generate
    Generate a PM checklist document for a machine + interval.

    Flow (matching data flow diagram):
    1. Validate RBAC
    2. Query PM Library for tasks
    3. Build document (PDF/DOCX/XLSX)
    4. Upload to storage (Azure/FTP/Local)
    5. Log to History Module
    6. Return download URL + metadata
    """
    # RBAC check
    user.require("pm:generate")

    # Validate machine exists
    machine = await crud.get_machine(db, req.machine_id)
    if not machine:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Machine '{req.machine_id}' not found in library",
        )

    # Query PM Library
    tasks = await crud.get_tasks_for_interval(db, req.machine_id, req.interval_hours)
    if not tasks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No tasks found for machine='{req.machine_id}' "
                f"interval={req.interval_hours}hr. "
                "Please upload the manual and add tasks to the library first."
            ),
        )

    interval_label = await get_interval_label(db, req.machine_id, req.interval_hours)
    now = datetime.utcnow()

    # Build file name — sanitized
    safe_machine = sanitize_filename(req.machine_id)
    safe_wo = sanitize_filename(req.work_order)
    ext = req.output_format
    file_name = f"PM_{safe_machine}_{req.interval_hours}hr_{now.strftime('%Y%m%d_%H%M%S')}_{safe_wo}.{ext}"

    # Extract parts list from tasks
    parts = extract_parts_from_tasks(tasks)

    # Build document model
    pm_doc = PMDocument(
        machine_name=machine.name,
        machine_id=req.machine_id,
        interval_hours=req.interval_hours,
        interval_label=interval_label,
        work_order=req.work_order,
        technician_name=req.technician_name,
        tasks=tasks,
        parts=parts,
        generated_at=now,
        watermark_text=now.strftime("%Y-%m-%d %H:%M UTC"),
        notes=req.notes or "",
    )

    # Generate document to temp file
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file_name

        if req.output_format == "pdf":
            out_path = generate_pdf(pm_doc, tmp_path)
        elif req.output_format == "docx":
            out_path = generate_docx(pm_doc, tmp_path)
        else:
            out_path = generate_xlsx(pm_doc, tmp_path)

        file_size = out_path.stat().st_size
        file_hash = compute_file_hash(out_path)

        # Upload to storage
        storage_target = req.storage_target or settings.default_storage_target
        try:
            used_target, download_url = await upload_file(
                out_path, req.machine_id, file_name, storage_target
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Storage upload failed: {exc}",
            )

    # Log to History Module — immutable record
    record = await crud.create_pm_record(
        db,
        {
            "machine_id": req.machine_id,
            "interval_hours": req.interval_hours,
            "work_order": req.work_order,
            "technician_name": req.technician_name,
            "technician_id": req.technician_id or user.user_id,
            "status": "COMPLETED",
            "started_at": now,
            "completed_at": now,
            "storage_target": used_target,
            "blob_url": download_url,
            "file_name": file_name,
            "file_hash": file_hash,
            "file_size_bytes": file_size,
            "notes": req.notes,
        },
    )

    # Audit log
    await log_action(
        db,
        action="PM_GENERATED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="pm_record",
        resource_id=record.record_id,
        details={
            "machine_id": req.machine_id,
            "interval_hours": req.interval_hours,
            "work_order": req.work_order,
            "task_count": len(tasks),
            "format": req.output_format,
            "storage_target": used_target,
        },
        ip_address=user.ip_address,
    )

    return GenerateResponse(
        record_id=record.record_id,
        machine_id=req.machine_id,
        machine_name=machine.name,
        interval_hours=req.interval_hours,
        interval_label=interval_label,
        work_order=req.work_order,
        technician_name=req.technician_name,
        file_name=file_name,
        file_size_bytes=file_size,
        file_hash=file_hash,
        storage_target=used_target,
        download_url=download_url,
        task_count=len(tasks),
        status=record.status,
        created_at=record.created_at.isoformat(),
    )
