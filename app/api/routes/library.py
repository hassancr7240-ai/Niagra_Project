from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.schemas.library import (
    AddTasksRequest,
    IntervalSummary,
    IntervalTasksResponse,
    LibraryResponse,
    MachineSummary,
    MachineHoursUpdate,
)
from app.schemas.tasks import TaskCreate, TaskResponse
from app.utils.audit import log_action

router = APIRouter(prefix="/api/library", tags=["PM Library"])


@router.get("", response_model=LibraryResponse)
async def get_library(user: CurrentUserDep, db: DBDep) -> LibraryResponse:
    """GET /api/library — returns all machines, intervals and task counts."""
    user.require("library:read")

    machines = await crud.get_all_machines(db)
    summary_map = await crud.get_library_summary(db)

    task_count_map: dict[tuple, int] = {
        (s["machine_id"], s["interval_hours"]): s["task_count"] for s in summary_map
    }
    total_tasks = sum(s["task_count"] for s in summary_map)

    machine_responses = []
    for m in machines:
        intervals = await crud.get_intervals_for_machine(db, m.machine_id)
        machine_total = sum(
            task_count_map.get((m.machine_id, iv.hours), 0) for iv in intervals
        )
        interval_summaries = [
            IntervalSummary(
                interval_id=iv.interval_id,
                machine_id=iv.machine_id,
                hours=iv.hours,
                label=iv.label,
                natural_label=iv.natural_label,
                source=iv.source,
                task_count=task_count_map.get((iv.machine_id, iv.hours), 0),
            )
            for iv in intervals
        ]
        machine_responses.append(
            MachineSummary(
                machine_id=m.machine_id,
                name=m.name,
                manufacturer=m.manufacturer,
                model=m.model,
                machine_type=m.machine_type,
                description=m.description,
                location=m.location,
                asset_tag=m.asset_tag,
                is_active=m.is_active,
                intervals=interval_summaries,
                total_tasks=machine_total,
            )
        )

    return LibraryResponse(
        machines=machine_responses,
        total_machines=len(machine_responses),
        total_tasks=total_tasks,
        total_intervals=sum(len(m.intervals) for m in machine_responses),
    )


@router.get("/{machine_id}/{interval_hours}", response_model=IntervalTasksResponse)
async def get_interval_tasks(
    machine_id: str,
    interval_hours: int,
    user: CurrentUserDep,
    db: DBDep,
) -> IntervalTasksResponse:
    """Get all tasks for a specific machine + interval."""
    user.require("library:read")

    machine = await crud.get_machine(db, machine_id)
    if not machine:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    tasks = await crud.get_tasks_for_interval(db, machine_id, interval_hours)
    intervals = await crud.get_intervals_for_machine(db, machine_id)
    label = next((iv.label for iv in intervals if iv.hours == interval_hours), f"{interval_hours}hr")

    task_responses = [
        TaskResponse(
            task_id=t.task_id,
            machine_id=t.machine_id,
            interval_hours=t.interval_hours,
            task_no=t.task_no,
            area=t.area,
            action=t.action,
            description=t.description,
            machine_state=t.machine_state,
            safety_flag=t.safety_flag,
            part_number=t.part_number,
            source_chapter=t.source_chapter,
            source_section=t.source_section,
            is_active=t.is_active,
        )
        for t in tasks
    ]

    return IntervalTasksResponse(
        machine_id=machine_id,
        machine_name=machine.name,
        interval_hours=interval_hours,
        interval_label=label,
        tasks=task_responses,
        task_count=len(task_responses),
    )


@router.post("/tasks", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_tasks(
    body: AddTasksRequest,
    user: CurrentUserDep,
    db: DBDep,
) -> dict:
    """
    POST /api/library/tasks
    Add tasks to the PM library. Engineers and Managers only.
    All changes are audit logged per the architecture diagram.
    """
    user.require("library:write")

    machine = await crud.get_machine(db, body.machine_id)
    if not machine:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    import uuid
    created_count = 0
    for t in body.tasks:
        try:
            validated = TaskCreate(
                machine_id=body.machine_id,
                interval_hours=body.interval_hours,
                **t,
            )
            await crud.create_task(
                db,
                {
                    "task_id": str(uuid.uuid4()),
                    "machine_id": validated.machine_id,
                    "interval_hours": validated.interval_hours,
                    "task_no": validated.task_no,
                    "area": validated.area,
                    "action": validated.action,
                    "description": validated.description,
                    "machine_state": validated.machine_state,
                    "safety_flag": validated.safety_flag,
                    "part_number": validated.part_number,
                    "source_chapter": validated.source_chapter,
                    "source_section": validated.source_section,
                },
            )
            created_count += 1
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Task validation failed: {exc}",
            )

    await log_action(
        db,
        action="LIBRARY_TASKS_ADDED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="library",
        resource_id=f"{body.machine_id}/{body.interval_hours}",
        details={"tasks_added": created_count},
        ip_address=user.ip_address,
    )

    return {"added": created_count, "machine_id": body.machine_id, "interval_hours": body.interval_hours}


@router.post("/hours", response_model=dict)
async def update_machine_hours(
    body: MachineHoursUpdate,
    user: CurrentUserDep,
    db: DBDep,
) -> dict:
    """Update current machine operating hours."""
    user.require("hours:write")

    machine = await crud.get_machine(db, body.machine_id)
    if not machine:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    await crud.record_machine_hours(
        db,
        {
            "machine_id": body.machine_id,
            "current_hours": body.current_hours,
            "recorded_by": user.email,
        },
    )
    return {"machine_id": body.machine_id, "current_hours": body.current_hours}
