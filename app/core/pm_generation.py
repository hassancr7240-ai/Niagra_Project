from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.core.document_generator import PMDocument, generate_docx, generate_pdf, generate_xlsx, generate_xlsx_all_intervals
from app.core.pm_library import extract_parts_from_tasks, get_interval_label
from app.core.storage_module import upload_file
from app.db import crud
from app.utils.audit import log_action
from app.utils.security import compute_file_hash, sanitize_filename

settings = get_settings()


@dataclass
class _ExtractedTask:
    """Lightweight stand-in for app.db.models.Task — used to render documents
    from RAG-extracted tasks (uploaded manuals) that aren't in the PM Library yet."""
    task_no: int
    area: str
    action: str
    description: str
    machine_state: str
    safety_flag: bool
    part_number: Optional[str]
    interval_hours: int = 0


async def generate_pm_document(
    db,
    user,
    machine_id: str,
    interval_hours: int,
    output_format: str = "pdf",
    work_order: Optional[str] = None,
    technician_name: Optional[str] = None,
    notes: Optional[str] = None,
    storage_target: Optional[str] = None,
) -> Optional[dict]:
    """
    Build, store, and log a PM checklist document (PDF/DOCX/XLSX).

    Shared by the POST /api/generate route and the conversational chat
    assistant — both produce the same audited, downloadable output.
    Returns None when the machine or interval has no tasks in the library.
    """
    machine = await crud.get_machine(db, machine_id)
    if not machine:
        return None

    tasks = await crud.get_tasks_for_interval(db, machine_id, interval_hours)
    if not tasks:
        return None

    interval_label = await get_interval_label(db, machine_id, interval_hours)
    now = datetime.utcnow()
    work_order = work_order or f"CHAT-{now.strftime('%Y%m%d%H%M%S')}"
    technician_name = technician_name or user.name or user.email

    safe_machine = sanitize_filename(machine_id)
    safe_wo = sanitize_filename(work_order)
    file_name = (
        f"PM_{safe_machine}_{interval_hours}hr_{now.strftime('%Y%m%d_%H%M%S')}_{safe_wo}.{output_format}"
    )

    parts = extract_parts_from_tasks(tasks)
    pm_doc = PMDocument(
        machine_name=machine.name,
        machine_id=machine_id,
        interval_hours=interval_hours,
        interval_label=interval_label,
        work_order=work_order,
        technician_name=technician_name,
        tasks=tasks,
        parts=parts,
        generated_at=now,
        watermark_text=now.strftime("%Y-%m-%d %H:%M UTC"),
        notes=notes or "",
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file_name
        if output_format == "pdf":
            out_path = generate_pdf(pm_doc, tmp_path)
        elif output_format == "docx":
            out_path = generate_docx(pm_doc, tmp_path)
        else:
            out_path = generate_xlsx(pm_doc, tmp_path)

        file_size = out_path.stat().st_size
        file_hash = compute_file_hash(out_path)
        used_target, download_url = await upload_file(
            out_path, machine_id, file_name, storage_target or settings.default_storage_target
        )

    record = await crud.create_pm_record(
        db,
        {
            "machine_id": machine_id,
            "interval_hours": interval_hours,
            "work_order": work_order,
            "technician_name": technician_name,
            "technician_id": user.user_id,
            "status": "COMPLETED",
            "started_at": now,
            "completed_at": now,
            "storage_target": used_target,
            "blob_url": download_url,
            "file_name": file_name,
            "file_hash": file_hash,
            "file_size_bytes": file_size,
            "notes": notes,
        },
    )

    await log_action(
        db,
        action="PM_GENERATED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="pm_record",
        resource_id=record.record_id,
        details={
            "machine_id": machine_id,
            "interval_hours": interval_hours,
            "work_order": work_order,
            "task_count": len(tasks),
            "format": output_format,
            "storage_target": used_target,
            "source": "chat",
        },
        ip_address=getattr(user, "ip_address", None),
    )

    return {
        "record_id": record.record_id,
        "machine_id": machine_id,
        "machine_name": machine.name,
        "interval_hours": interval_hours,
        "interval_label": interval_label,
        "file_name": file_name,
        "file_size_bytes": file_size,
        "download_url": download_url,
        "task_count": len(tasks),
        "output_format": output_format,
    }


async def generate_pm_document_from_manual(
    db,
    user,
    manual_id: str,
    output_format: str = "pdf",
    work_order: Optional[str] = None,
    technician_name: Optional[str] = None,
    storage_target: Optional[str] = None,
) -> Optional[dict]:
    """
    Build, store, and log a PM checklist document (PDF/DOCX/XLSX) using the
    AI-extracted tasks from an uploaded manual — same template as
    generate_pm_document, but for manuals not yet approved into the PM Library.
    Returns None if the manual isn't processed yet or has no extracted tasks.
    """
    upload = await crud.get_manual_upload(db, manual_id)
    if not upload or upload.status not in ("PENDING_REVIEW", "APPROVED"):
        return None

    raw_tasks = json.loads(upload.extracted_tasks or "[]")
    if not raw_tasks:
        return None

    tasks = [
        _ExtractedTask(
            task_no=t.get("task_no", (i + 1) * 10),
            area=t.get("area", "GENERAL"),
            action=t.get("action", "CHECK"),
            description=t.get("description", ""),
            machine_state=t.get("machine_state", "STOPPED"),
            safety_flag=bool(t.get("safety_flag", False)),
            part_number=t.get("part_number"),
            interval_hours=int(t.get("interval_hours", 0) or 0),
        )
        for i, t in enumerate(raw_tasks)
    ]

    now = datetime.utcnow()
    work_order = work_order or f"CHAT-{now.strftime('%Y%m%d%H%M%S')}"
    technician_name = technician_name or user.name or user.email

    machine_label = upload.machine_id or upload.detected_manufacturer or "EQUIPMENT"
    manual_stem = sanitize_filename(Path(upload.original_filename).stem)
    safe_wo = sanitize_filename(work_order)
    file_name = f"PM_{manual_stem}_{now.strftime('%Y%m%d_%H%M%S')}_{safe_wo}.{output_format}"

    parts = extract_parts_from_tasks(tasks)
    pm_doc = PMDocument(
        machine_name=machine_label,
        machine_id=upload.machine_id or f"MANUAL-{manual_id[:8]}",
        interval_hours=0,
        interval_label="Manual-Derived",
        work_order=work_order,
        technician_name=technician_name,
        tasks=tasks,
        parts=parts,
        generated_at=now,
        watermark_text=now.strftime("%Y-%m-%d %H:%M UTC"),
        notes=f"Generated from uploaded manual: {upload.original_filename}",
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file_name
        if output_format == "pdf":
            out_path = generate_pdf(pm_doc, tmp_path)
        elif output_format == "docx":
            out_path = generate_docx(pm_doc, tmp_path)
        else:
            out_path = generate_xlsx(pm_doc, tmp_path)

        file_size = out_path.stat().st_size
        file_hash = compute_file_hash(out_path)
        used_target, download_url = await upload_file(
            out_path, pm_doc.machine_id, file_name, storage_target or settings.default_storage_target
        )

    record = await crud.create_pm_record(
        db,
        {
            "machine_id": pm_doc.machine_id,
            "interval_hours": 0,
            "work_order": work_order,
            "technician_name": technician_name,
            "technician_id": user.user_id,
            "status": "COMPLETED",
            "started_at": now,
            "completed_at": now,
            "storage_target": used_target,
            "blob_url": download_url,
            "file_name": file_name,
            "file_hash": file_hash,
            "file_size_bytes": file_size,
            "notes": f"Generated from uploaded manual: {upload.original_filename}",
        },
    )

    await log_action(
        db,
        action="PM_GENERATED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="pm_record",
        resource_id=record.record_id,
        details={
            "manual_id": manual_id,
            "machine_id": pm_doc.machine_id,
            "work_order": work_order,
            "task_count": len(tasks),
            "format": output_format,
            "storage_target": used_target,
            "source": "chat_pdf_mode",
        },
        ip_address=getattr(user, "ip_address", None),
    )

    return {
        "record_id": record.record_id,
        "machine_id": pm_doc.machine_id,
        "machine_name": machine_label,
        "file_name": file_name,
        "file_size_bytes": file_size,
        "download_url": download_url,
        "task_count": len(tasks),
        "output_format": output_format,
        "source_manual": upload.original_filename,
    }


async def generate_pm_xlsx_per_interval(
    db,
    user,
    manual_id: str,
    work_order: Optional[str] = None,
    technician_name: Optional[str] = None,
    storage_target: Optional[str] = None,
) -> list[dict]:
    """
    Generate one XLSX file per PM interval from an uploaded manual's extracted tasks.
    Returns a list of result dicts (one per interval file), empty list if nothing available.
    """
    upload = await crud.get_manual_upload(db, manual_id)
    if not upload or upload.status not in ("PENDING_REVIEW", "APPROVED"):
        return []

    raw_tasks = json.loads(upload.extracted_tasks or "[]")
    if not raw_tasks:
        return []

    tasks = [
        _ExtractedTask(
            task_no=t.get("task_no", (i + 1) * 10),
            area=t.get("area", "GENERAL"),
            action=t.get("action", "CHECK"),
            description=t.get("description", ""),
            machine_state=t.get("machine_state", "STOPPED"),
            safety_flag=bool(t.get("safety_flag", False)),
            part_number=t.get("part_number"),
            interval_hours=int(t.get("interval_hours", 0) or 0),
        )
        for i, t in enumerate(raw_tasks)
    ]

    now = datetime.utcnow()
    work_order = work_order or f"CHAT-{now.strftime('%Y%m%d%H%M%S')}"
    technician_name = technician_name or user.name or user.email

    machine_label = upload.machine_id or upload.detected_manufacturer or "EQUIPMENT"
    machine_id_str = upload.machine_id or f"MANUAL-{manual_id[:8]}"

    pm_doc = PMDocument(
        machine_name=machine_label,
        machine_id=machine_id_str,
        interval_hours=0,
        interval_label="All Intervals",
        work_order=work_order,
        technician_name=technician_name,
        tasks=tasks,
        parts=extract_parts_from_tasks(tasks),
        generated_at=now,
        watermark_text=now.strftime("%Y-%m-%d %H:%M UTC"),
        notes=f"Generated from: {upload.original_filename}",
    )

    results: list[dict] = []
    used_target = storage_target or settings.default_storage_target

    with tempfile.TemporaryDirectory() as tmp_dir:
        interval_files = generate_xlsx_all_intervals(pm_doc, Path(tmp_dir))
        for file_path, interval_label, interval_hours in interval_files:
            file_name = file_path.name
            file_size = file_path.stat().st_size
            file_hash = compute_file_hash(file_path)
            used_target, download_url = await upload_file(
                file_path, machine_id_str, file_name,
                storage_target or settings.default_storage_target,
            )
            record = await crud.create_pm_record(
                db,
                {
                    "machine_id": machine_id_str,
                    "interval_hours": interval_hours,
                    "work_order": work_order,
                    "technician_name": technician_name,
                    "technician_id": user.user_id,
                    "status": "COMPLETED",
                    "started_at": now,
                    "completed_at": now,
                    "storage_target": used_target,
                    "blob_url": download_url,
                    "file_name": file_name,
                    "file_hash": file_hash,
                    "file_size_bytes": file_size,
                    "notes": f"Generated from: {upload.original_filename}",
                },
            )
            task_count = sum(
                1 for t in tasks if getattr(t, "interval_hours", 0) == interval_hours
            )
            results.append({
                "record_id": record.record_id,
                "machine_id": machine_id_str,
                "machine_name": machine_label,
                "interval_hours": interval_hours,
                "interval_label": interval_label,
                "file_name": file_name,
                "file_size_bytes": file_size,
                "download_url": download_url,
                "task_count": task_count,
                "source_manual": upload.original_filename,
            })

    if results:
        await log_action(
            db,
            action="PM_GENERATED",
            user_id=user.user_id,
            user_email=user.email,
            resource_type="manual_upload",
            resource_id=manual_id,
            details={
                "manual_id": manual_id,
                "machine_id": machine_id_str,
                "work_order": work_order,
                "file_count": len(results),
                "format": "xlsx",
                "storage_target": used_target,
                "source": "xlsx_per_interval",
            },
            ip_address=getattr(user, "ip_address", None),
        )

    return results
