from __future__ import annotations

"""
Export routes — Manager full read + export access.
Architecture: Manager → View History → Full read + export access
"""

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.utils.audit import log_action

router = APIRouter(prefix="/api/export", tags=["Export"])


@router.get("/history/csv")
async def export_history_csv(
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
) -> StreamingResponse:
    """
    GET /api/export/history/csv
    Manager exports full PM history as CSV for compliance / audit reporting.
    Architecture: Manager → Full read + export access
    """
    user.require("history:export")

    records, total = await crud.get_pm_history(
        db,
        machine_id=machine_id,
        from_date=from_date,
        to_date=to_date,
        limit=10000,
    )

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Record ID", "Machine ID", "Machine Name", "Interval (hrs)", "Interval Label",
        "Work Order", "Technician", "Status", "Storage Target",
        "File Name", "File Size (bytes)", "File Hash (SHA-256)",
        "Started At", "Completed At", "Approved By", "Approved At",
        "Created At", "Notes",
    ])

    for r in records:
        machine_name = r.machine.name if r.machine else r.machine_id
        writer.writerow([
            r.record_id,
            r.machine_id,
            machine_name,
            r.interval_hours,
            f"{r.interval_hours}hr",
            r.work_order,
            r.technician_name,
            r.status,
            r.storage_target or "",
            r.file_name or "",
            r.file_size_bytes or "",
            r.file_hash or "",
            r.started_at.isoformat() if r.started_at else "",
            r.completed_at.isoformat() if r.completed_at else "",
            r.approved_by or "",
            r.approved_at.isoformat() if r.approved_at else "",
            r.created_at.isoformat(),
            r.notes or "",
        ])

    await log_action(
        db,
        action="HISTORY_EXPORTED_CSV",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="export",
        details={"records_exported": len(records), "machine_id": machine_id},
        ip_address=user.ip_address,
    )

    output.seek(0)
    filename = f"PM_History_Export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/library/csv")
async def export_library_csv(
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = Query(None),
) -> StreamingResponse:
    """Export PM Library tasks as CSV — full task register for compliance."""
    user.require("history:export")

    machines = await crud.get_all_machines(db)
    if machine_id:
        machines = [m for m in machines if m.machine_id == machine_id]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Machine ID", "Machine Name", "Interval (hrs)", "Interval Label",
        "Task No", "Area", "Action", "Description",
        "Machine State", "Safety Flag", "Part Number",
        "Source Chapter", "Source Section",
    ])

    for m in machines:
        intervals = await crud.get_intervals_for_machine(db, m.machine_id)
        for iv in intervals:
            tasks = await crud.get_tasks_for_interval(db, m.machine_id, iv.hours)
            for t in tasks:
                writer.writerow([
                    m.machine_id, m.name, iv.hours, iv.label,
                    t.task_no, t.area, t.action, t.description,
                    t.machine_state, "YES" if t.safety_flag else "NO",
                    t.part_number or "",
                    t.source_chapter or "", t.source_section or "",
                ])

    output.seek(0)
    filename = f"PM_Library_Export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit-logs/csv")
async def export_audit_logs_csv(
    user: CurrentUserDep,
    db: DBDep,
    resource_type: Optional[str] = Query(None),
) -> StreamingResponse:
    """
    Export immutable audit logs as CSV.
    Architecture: Audit Logs — every action logged, retained per GMP compliance.
    """
    user.require("audit:read")

    logs = await crud.get_audit_logs(db, resource_type=resource_type, limit=50000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Log ID", "Timestamp", "User ID", "User Email",
        "Action", "Resource Type", "Resource ID", "Details", "IP Address",
    ])
    for log in logs:
        writer.writerow([
            log.log_id, log.timestamp.isoformat(),
            log.user_id or "", log.user_email or "",
            log.action, log.resource_type or "", log.resource_id or "",
            log.details or "", log.ip_address or "",
        ])

    output.seek(0)
    filename = f"PM_AuditLog_Export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
