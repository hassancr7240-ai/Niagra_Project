from __future__ import annotations

"""
Checklist completion routes — Technician fills checklist in dashboard.
Architecture: Technician → Fill Checklist → Read/Write/submit only
Data Flow: Response Sent to Dashboard → END
"""

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db import crud
from app.db.models import CompletedTask
from app.dependencies import CurrentUserDep, DBDep
from app.utils.audit import log_action
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/checklist", tags=["Checklist"])


class TaskCompletionItem(BaseModel):
    task_id: str
    task_no: int
    initialed_by: str
    is_done: bool
    notes: Optional[str] = None


class ChecklistSubmission(BaseModel):
    completed_tasks: list[TaskCompletionItem]
    hours_at_completion: Optional[int] = None
    notes: Optional[str] = None


class ChecklistResponse(BaseModel):
    record_id: str
    completed_count: int
    total_tasks: int
    completion_percentage: float
    status: str


@router.post("/{record_id}", response_model=ChecklistResponse)
async def submit_checklist(
    record_id: str,
    body: ChecklistSubmission,
    user: CurrentUserDep,
    db: DBDep,
) -> ChecklistResponse:
    """
    POST /api/checklist/{record_id}
    Technician submits completed PM checklist.
    Architecture: Technician → Fill Checklist → Read/Write/submit only
    """
    user.require("pm:complete")

    record = await crud.get_pm_record(db, record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PM record not found")

    if record.status == "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify an approved PM record",
        )

    # Get total task count for this interval
    tasks = await crud.get_tasks_for_interval(db, record.machine_id, record.interval_hours)
    total_tasks = len(tasks)

    # Save/update completed task records
    completed_count = 0
    for item in body.completed_tasks:
        # Upsert completed task
        existing_result = await db.execute(
            select(CompletedTask).where(
                CompletedTask.record_id == record_id,
                CompletedTask.task_id == item.task_id,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            existing.initialed_by = item.initialed_by
            existing.is_done = item.is_done
            existing.notes = item.notes
            existing.completed_at = datetime.utcnow() if item.is_done else None
        else:
            ct = CompletedTask(
                record_id=record_id,
                task_id=item.task_id,
                task_no=item.task_no,
                initialed_by=item.initialed_by,
                is_done=item.is_done,
                notes=item.notes,
                completed_at=datetime.utcnow() if item.is_done else None,
            )
            db.add(ct)

        if item.is_done:
            completed_count += 1

    # Determine new status
    completion_pct = (completed_count / total_tasks * 100) if total_tasks else 0
    new_status = "IN_PROGRESS"
    if completed_count == total_tasks and total_tasks > 0:
        new_status = "COMPLETED"

    update_data: dict = {"status": new_status}
    if new_status == "COMPLETED":
        update_data["completed_at"] = datetime.utcnow()
    if body.notes:
        update_data["notes"] = body.notes

    await crud.update_pm_record(db, record_id, update_data)
    await db.flush()

    await log_action(
        db,
        action="CHECKLIST_SUBMITTED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="pm_record",
        resource_id=record_id,
        details={
            "completed_count": completed_count,
            "total_tasks": total_tasks,
            "completion_pct": round(completion_pct, 1),
            "new_status": new_status,
        },
        ip_address=user.ip_address,
    )

    return ChecklistResponse(
        record_id=record_id,
        completed_count=completed_count,
        total_tasks=total_tasks,
        completion_percentage=round(completion_pct, 1),
        status=new_status,
    )


@router.get("/{record_id}", response_model=dict)
async def get_checklist_status(
    record_id: str,
    user: CurrentUserDep,
    db: DBDep,
) -> dict:
    """Get current checklist completion status for a PM record."""
    user.require("pm:read")

    record = await crud.get_pm_record(db, record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PM record not found")

    tasks = await crud.get_tasks_for_interval(db, record.machine_id, record.interval_hours)

    completed_result = await db.execute(
        select(CompletedTask).where(CompletedTask.record_id == record_id)
    )
    completed_tasks = completed_result.scalars().all()

    completed_map = {ct.task_id: ct for ct in completed_tasks}
    task_statuses = []
    for t in tasks:
        ct = completed_map.get(t.task_id)
        task_statuses.append({
            "task_id": t.task_id,
            "task_no": t.task_no,
            "area": t.area,
            "action": t.action,
            "description": t.description,
            "machine_state": t.machine_state,
            "safety_flag": t.safety_flag,
            "part_number": t.part_number,
            "is_done": ct.is_done if ct else False,
            "initialed_by": ct.initialed_by if ct else None,
            "notes": ct.notes if ct else None,
        })

    done_count = sum(1 for t in task_statuses if t["is_done"])
    return {
        "record_id": record_id,
        "machine_id": record.machine_id,
        "interval_hours": record.interval_hours,
        "work_order": record.work_order,
        "status": record.status,
        "total_tasks": len(tasks),
        "completed_tasks": done_count,
        "completion_percentage": round(done_count / len(tasks) * 100, 1) if tasks else 0,
        "tasks": task_statuses,
    }
