from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import crud
from app.db.models import Machine, PMInterval, PMRecord
from app.schemas.history import (
    DashboardResponse,
    DashboardStats,
    OverdueItem,
    PMRecordResponse,
    ScheduleItem,
)


async def build_dashboard(db: AsyncSession) -> DashboardResponse:
    machines = await crud.get_all_machines(db)
    now = datetime.utcnow()

    # Stats
    records, total = await crud.get_pm_history(db, limit=1000)
    completed_this_month = sum(
        1 for r in records
        if r.completed_at and r.completed_at.month == now.month
        and r.completed_at.year == now.year
    )

    overdue_items: list[OverdueItem] = []
    schedule_items: list[ScheduleItem] = []

    for machine in machines:
        machine_hours_rec = await crud.get_latest_machine_hours(db, machine.machine_id)
        current_hours = machine_hours_rec.current_hours if machine_hours_rec else None
        intervals = await crud.get_intervals_for_machine(db, machine.machine_id)

        for interval in intervals:
            last_pm = await crud.get_last_pm_for_interval(db, machine.machine_id, interval.hours)
            status = _calculate_status(current_hours, last_pm, interval.hours)

            schedule_items.append(
                ScheduleItem(
                    machine_id=machine.machine_id,
                    machine_name=machine.name,
                    interval_hours=interval.hours,
                    interval_label=interval.label,
                    last_completed_at=last_pm.completed_at if last_pm else None,
                    predicted_next_due_hours=_predict_next_due(current_hours, last_pm, interval.hours),
                    status=status,
                )
            )

            if status == "OVERDUE":
                hours_overdue = None
                if current_hours is not None and last_pm:
                    hours_since = current_hours - (last_pm.completed_at.timestamp() if last_pm.completed_at else 0)
                    hours_overdue = max(0, int(current_hours - interval.hours))
                overdue_items.append(
                    OverdueItem(
                        machine_id=machine.machine_id,
                        machine_name=machine.name,
                        interval_hours=interval.hours,
                        interval_label=interval.label,
                        last_completed_at=last_pm.completed_at if last_pm else None,
                        hours_overdue=hours_overdue,
                        current_machine_hours=current_hours,
                    )
                )

    summary = await crud.get_library_summary(db)
    total_tasks = sum(s["task_count"] for s in summary)

    recent, _ = await crud.get_pm_history(db, limit=10)
    recent_responses = [await _pm_record_to_response(db, r) for r in recent]

    stats = DashboardStats(
        total_pms=total,
        completed_this_month=completed_this_month,
        overdue_count=len(overdue_items),
        due_soon_count=sum(1 for s in schedule_items if s.status == "DUE_SOON"),
        machines_covered=len(machines),
        total_tasks=total_tasks,
    )

    return DashboardResponse(
        stats=stats,
        overdue=overdue_items,
        schedule=schedule_items,
        recent_pms=recent_responses,
    )


def _calculate_status(
    current_hours: Optional[int],
    last_pm: Optional[PMRecord],
    interval_hours: int,
) -> str:
    if current_hours is None:
        return "NEVER_DONE" if not last_pm else "ON_TRACK"
    if not last_pm:
        return "OVERDUE" if current_hours >= interval_hours else "DUE_SOON"
    # Use completed PM count as proxy for cycles completed
    # In a real system you'd track machine hours at time of PM
    return "ON_TRACK"


def _predict_next_due(
    current_hours: Optional[int],
    last_pm: Optional[PMRecord],
    interval_hours: int,
) -> Optional[int]:
    if current_hours is None:
        return None
    if last_pm and last_pm.completed_at:
        return interval_hours
    return max(0, interval_hours - (current_hours or 0))


async def _pm_record_to_response(db: AsyncSession, record: PMRecord) -> PMRecordResponse:
    from app.db.models import PMInterval
    machine = record.machine or await crud.get_machine(db, record.machine_id)
    machine_name = machine.name if machine else record.machine_id

    interval_label = f"{record.interval_hours}hr"
    return PMRecordResponse(
        record_id=record.record_id,
        machine_id=record.machine_id,
        machine_name=machine_name,
        interval_hours=record.interval_hours,
        interval_label=interval_label,
        work_order=record.work_order,
        technician_name=record.technician_name,
        status=record.status,
        storage_target=record.storage_target,
        download_url=record.blob_url,
        file_name=record.file_name,
        started_at=record.started_at,
        completed_at=record.completed_at,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        created_at=record.created_at,
    )
