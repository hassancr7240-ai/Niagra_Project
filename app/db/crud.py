from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update, delete, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AuditLog,
    Citation,
    CompletedTask,
    Machine,
    MachineHours,
    ManualApproval,
    ManualUpload,
    PMInterval,
    PMRecord,
    Task,
    User,
)


# ─── Machines ─────────────────────────────────────────────────────────────────

async def get_machine(db: AsyncSession, machine_id: str) -> Optional[Machine]:
    result = await db.execute(select(Machine).where(Machine.machine_id == machine_id))
    return result.scalar_one_or_none()


async def get_all_machines(db: AsyncSession) -> list[Machine]:
    result = await db.execute(
        select(Machine).where(Machine.is_active == True).order_by(Machine.name)
    )
    return list(result.scalars().all())


async def create_machine(db: AsyncSession, data: dict) -> Machine:
    m = Machine(**data)
    db.add(m)
    await db.flush()
    return m


async def update_machine(db: AsyncSession, machine_id: str, data: dict) -> Optional[Machine]:
    await db.execute(
        update(Machine).where(Machine.machine_id == machine_id).values(**data)
    )
    return await get_machine(db, machine_id)


# ─── Tasks ────────────────────────────────────────────────────────────────────

async def get_tasks_for_interval(
    db: AsyncSession, machine_id: str, interval_hours: int
) -> list[Task]:
    result = await db.execute(
        select(Task)
        .where(
            and_(
                Task.machine_id == machine_id,
                Task.interval_hours == interval_hours,
                Task.is_active == True,
            )
        )
        .order_by(Task.task_no)
    )
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: str) -> Optional[Task]:
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def create_task(db: AsyncSession, data: dict) -> Task:
    t = Task(**data)
    db.add(t)
    await db.flush()
    return t


async def get_task_count(db: AsyncSession, machine_id: str) -> int:
    result = await db.execute(
        select(func.count(Task.task_id)).where(
            and_(Task.machine_id == machine_id, Task.is_active == True)
        )
    )
    return result.scalar_one() or 0


async def get_library_summary(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(
            Task.machine_id,
            Task.interval_hours,
            func.count(Task.task_id).label("task_count"),
        )
        .where(Task.is_active == True)
        .group_by(Task.machine_id, Task.interval_hours)
        .order_by(Task.machine_id, Task.interval_hours)
    )
    return [{"machine_id": r.machine_id, "interval_hours": r.interval_hours, "task_count": r.task_count} for r in result.all()]


# ─── Intervals ────────────────────────────────────────────────────────────────

async def get_intervals_for_machine(db: AsyncSession, machine_id: str) -> list[PMInterval]:
    result = await db.execute(
        select(PMInterval)
        .where(PMInterval.machine_id == machine_id)
        .order_by(PMInterval.hours)
    )
    return list(result.scalars().all())


async def create_interval(db: AsyncSession, data: dict) -> PMInterval:
    obj = PMInterval(**data)
    db.add(obj)
    await db.flush()
    return obj


# ─── PM Records ───────────────────────────────────────────────────────────────

async def create_pm_record(db: AsyncSession, data: dict) -> PMRecord:
    rec = PMRecord(**data)
    db.add(rec)
    await db.flush()
    return rec


async def get_pm_record(db: AsyncSession, record_id: str) -> Optional[PMRecord]:
    result = await db.execute(
        select(PMRecord)
        .options(selectinload(PMRecord.machine))
        .where(PMRecord.record_id == record_id)
    )
    return result.scalar_one_or_none()


async def update_pm_record(db: AsyncSession, record_id: str, data: dict) -> Optional[PMRecord]:
    await db.execute(
        update(PMRecord).where(PMRecord.record_id == record_id).values(**data)
    )
    return await get_pm_record(db, record_id)


async def delete_pm_record(db: AsyncSession, record_id: str) -> bool:
    result = await db.execute(
        delete(PMRecord).where(PMRecord.record_id == record_id)
    )
    await db.commit()
    return result.rowcount > 0


async def get_pm_history(
    db: AsyncSession,
    machine_id: Optional[str] = None,
    technician_id: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[PMRecord], int]:
    q = select(PMRecord).options(selectinload(PMRecord.machine))
    filters = []
    if machine_id:
        filters.append(PMRecord.machine_id == machine_id)
    if technician_id:
        filters.append(PMRecord.technician_id == technician_id)
    if from_date:
        filters.append(PMRecord.created_at >= from_date)
    if to_date:
        filters.append(PMRecord.created_at <= to_date)
    if status:
        filters.append(PMRecord.status == status)
    if filters:
        q = q.where(and_(*filters))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one() or 0

    q = q.order_by(desc(PMRecord.created_at)).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all()), total


async def get_last_pm_for_interval(
    db: AsyncSession, machine_id: str, interval_hours: int
) -> Optional[PMRecord]:
    result = await db.execute(
        select(PMRecord)
        .where(
            and_(
                PMRecord.machine_id == machine_id,
                PMRecord.interval_hours == interval_hours,
                PMRecord.status.in_(["COMPLETED", "APPROVED"]),
            )
        )
        .order_by(desc(PMRecord.completed_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


# ─── Machine Hours ────────────────────────────────────────────────────────────

async def get_latest_machine_hours(db: AsyncSession, machine_id: str) -> Optional[MachineHours]:
    result = await db.execute(
        select(MachineHours)
        .where(MachineHours.machine_id == machine_id)
        .order_by(desc(MachineHours.recorded_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def record_machine_hours(db: AsyncSession, data: dict) -> MachineHours:
    obj = MachineHours(**data)
    db.add(obj)
    await db.flush()
    return obj


# ─── Users ────────────────────────────────────────────────────────────────────

async def get_user(db: AsyncSession, user_id: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.user_id == user_id))
    return result.scalar_one_or_none()


async def upsert_user(db: AsyncSession, data: dict) -> User:
    existing = await get_user(db, data["user_id"])
    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
        await db.flush()
        return existing
    u = User(**data)
    db.add(u)
    await db.flush()
    return u


# ─── Audit Logs ───────────────────────────────────────────────────────────────

async def write_audit_log(db: AsyncSession, data: dict) -> AuditLog:
    log = AuditLog(**data)
    db.add(log)
    await db.flush()
    return log


async def get_audit_logs(
    db: AsyncSession,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    limit: int = 200,
) -> list[AuditLog]:
    q = select(AuditLog)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    q = q.order_by(desc(AuditLog.timestamp)).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


# ─── Manual Uploads ───────────────────────────────────────────────────────────

async def create_manual_upload(db: AsyncSession, data: dict) -> ManualUpload:
    obj = ManualUpload(**data)
    db.add(obj)
    await db.flush()
    return obj


async def get_manual_upload(db: AsyncSession, manual_id: str) -> Optional[ManualUpload]:
    result = await db.execute(
        select(ManualUpload).where(ManualUpload.manual_id == manual_id)
    )
    return result.scalar_one_or_none()


async def delete_manual_upload(db: AsyncSession, manual_id: str) -> bool:
    result = await db.execute(
        delete(ManualUpload).where(ManualUpload.manual_id == manual_id)
    )
    await db.commit()
    return result.rowcount > 0


async def update_manual_upload(db: AsyncSession, manual_id: str, data: dict) -> Optional[ManualUpload]:
    await db.execute(
        update(ManualUpload).where(ManualUpload.manual_id == manual_id).values(**data)
    )
    return await get_manual_upload(db, manual_id)


async def get_manual_uploads(
    db: AsyncSession, status: Optional[str] = None, limit: int = 50
) -> list[ManualUpload]:
    q = select(ManualUpload)
    if status:
        q = q.where(ManualUpload.status == status)
    q = q.order_by(desc(ManualUpload.created_at)).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


# ─── Citations ────────────────────────────────────────────────────────────────

async def save_citations(db: AsyncSession, citations: list[dict]) -> int:
    """Bulk-insert citation records. Returns number inserted."""
    if not citations:
        return 0
    objs = [Citation(**c) for c in citations]
    db.add_all(objs)
    await db.flush()
    return len(objs)


async def get_citations_for_manual(
    db: AsyncSession, manual_id: str
) -> list[Citation]:
    result = await db.execute(
        select(Citation)
        .where(Citation.manual_id == manual_id)
        .order_by(Citation.page_start)
    )
    return list(result.scalars().all())


async def get_citations_by_content_type(
    db: AsyncSession, manual_id: str, content_type: str
) -> list[Citation]:
    result = await db.execute(
        select(Citation)
        .where(
            and_(Citation.manual_id == manual_id, Citation.content_type == content_type)
        )
        .order_by(Citation.page_start)
    )
    return list(result.scalars().all())


# ─── Manual Approvals ─────────────────────────────────────────────────────────

async def save_manual_approval(db: AsyncSession, data: dict) -> ManualApproval:
    obj = ManualApproval(**data)
    db.add(obj)
    await db.flush()
    return obj


async def get_manual_approvals(
    db: AsyncSession, manual_id: str
) -> list[ManualApproval]:
    result = await db.execute(
        select(ManualApproval)
        .where(ManualApproval.manual_id == manual_id)
        .order_by(desc(ManualApproval.created_at))
    )
    return list(result.scalars().all())


async def get_approval_for_interval(
    db: AsyncSession, manual_id: str, interval_hours: int
) -> Optional[ManualApproval]:
    """Get the latest approval/rejection for a specific interval of a manual."""
    result = await db.execute(
        select(ManualApproval)
        .where(
            and_(
                ManualApproval.manual_id == manual_id,
                ManualApproval.interval_hours == interval_hours,
            )
        )
        .order_by(desc(ManualApproval.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()
