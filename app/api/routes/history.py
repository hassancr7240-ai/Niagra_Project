from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, status

from app.core.analytics import analyse_pm_patterns
from app.core.history_module import build_dashboard, _pm_record_to_response
from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.schemas.history import (
    ApproveRequest,
    DashboardResponse,
    HistoryFilterParams,
    PMRecordResponse,
)
from app.utils.audit import log_action

router = APIRouter(prefix="/api/history", tags=["History"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    user: CurrentUserDep,
    db: DBDep,
) -> DashboardResponse:
    """
    GET /api/history/dashboard
    Data Flow Diagram — View History branch:
    GET /api/history → Query Azure SQL DB → IBM watsonx.ai Analytics
    (granite-13b-instruct-v2) Analyse PM Patterns + Predict Next Due
    → Return Dashboard Data (Stats / Overdue / Schedule)
    """
    user.require("dashboard:read")
    dashboard = await build_dashboard(db)

    # ── IBM watsonx.ai Analytics (Data Flow Diagram) ──────────────────────────
    # granite-13b-instruct-v2 → Analyse PM Patterns + Predict Next Due
    try:
        machines = await crud.get_all_machines(db)
        records, _ = await crud.get_pm_history(db, limit=100)

        current_hours_map = {}
        for m in machines:
            h = await crud.get_latest_machine_hours(db, m.machine_id)
            if h:
                current_hours_map[m.machine_id] = h.current_hours

        records_dicts = [
            {
                "machine_id": r.machine_id,
                "interval_hours": r.interval_hours,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "status": r.status,
            }
            for r in records
        ]
        machines_dicts = [
            {"machine_id": m.machine_id, "name": m.name} for m in machines
        ]

        analytics = await analyse_pm_patterns(
            records_dicts, machines_dicts, current_hours_map
        )

        # Merge analytics insights into dashboard (as extra metadata)
        dashboard.__dict__["analytics"] = analytics
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Analytics step failed: %s", exc)

    return dashboard


@router.get("", response_model=dict)
async def get_history(
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = Query(None),
    technician_id: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """GET /api/history — filter PM records by machine, date, technician."""
    user.require("history:read")

    records, total = await crud.get_pm_history(
        db,
        machine_id=machine_id,
        technician_id=technician_id,
        from_date=from_date,
        to_date=to_date,
        status=status_filter,
        limit=limit,
        offset=offset,
    )

    responses = [await _pm_record_to_response(db, r) for r in records]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": [r.model_dump() for r in responses],
    }


@router.get("/{record_id}", response_model=PMRecordResponse)
async def get_pm_record(
    record_id: str,
    user: CurrentUserDep,
    db: DBDep,
) -> PMRecordResponse:
    user.require("history:read")
    record = await crud.get_pm_record(db, record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    return await _pm_record_to_response(db, record)


@router.post("/{record_id}/approve", response_model=PMRecordResponse)
async def approve_pm_record(
    record_id: str,
    body: ApproveRequest,
    user: CurrentUserDep,
    db: DBDep,
) -> PMRecordResponse:
    """Supervisor/Manager approves a completed PM record."""
    user.require("pm:approve")

    record = await crud.get_pm_record(db, record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    if record.status not in ("COMPLETED", "PENDING"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve record with status '{record.status}'",
        )

    updated = await crud.update_pm_record(
        db,
        record_id,
        {
            "status": "APPROVED",
            "approved_by": user.email,
            "approved_at": datetime.utcnow(),
            "notes": body.notes,
        },
    )

    await log_action(
        db,
        action="PM_APPROVED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="pm_record",
        resource_id=record_id,
        details={"approved_by": user.email, "notes": body.notes},
        ip_address=user.ip_address,
    )

    return await _pm_record_to_response(db, updated)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pm_record(
    record_id: str,
    user: CurrentUserDep,
    db: DBDep,
) -> None:
    """Delete a PM record (Manager/Supervisor only)."""
    user.require("pm:approve")
    deleted = await crud.delete_pm_record(db, record_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    await log_action(
        db, action="PM_RECORD_DELETED", user_id=user.user_id, user_email=user.email,
        resource_type="pm_record", resource_id=record_id,
        details={"deleted_by": user.email}, ip_address=user.ip_address,
    )


@router.delete("", status_code=status.HTTP_200_OK)
async def delete_pm_records_bulk(
    record_ids: list[str] = Body(...),
    user: CurrentUserDep = None,
    db: DBDep = None,
) -> dict:
    """Bulk delete PM records by list of record_ids."""
    user.require("pm:approve")
    count = 0
    for rid in record_ids:
        if await crud.delete_pm_record(db, rid):
            count += 1
    return {"deleted": count}
