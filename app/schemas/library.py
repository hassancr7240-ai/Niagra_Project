from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from app.schemas.tasks import TaskResponse


class IntervalSummary(BaseModel):
    interval_id: str
    machine_id: str
    hours: int
    label: str
    natural_label: str
    source: Optional[str]
    task_count: int

    model_config = {"from_attributes": True}


class MachineSummary(BaseModel):
    machine_id: str
    name: str
    manufacturer: str
    model: str
    machine_type: str
    description: Optional[str]
    location: Optional[str]
    asset_tag: Optional[str]
    is_active: bool
    intervals: list[IntervalSummary] = []
    total_tasks: int = 0

    model_config = {"from_attributes": True}


class LibraryResponse(BaseModel):
    machines: list[MachineSummary]
    total_machines: int
    total_tasks: int
    total_intervals: int


class IntervalTasksResponse(BaseModel):
    machine_id: str
    machine_name: str
    interval_hours: int
    interval_label: str
    tasks: list[TaskResponse]
    task_count: int


class AddTasksRequest(BaseModel):
    machine_id: str
    interval_hours: int
    tasks: list[dict] = Field(..., description="List of task dicts matching TaskCreate schema")


class MachineHoursUpdate(BaseModel):
    machine_id: str
    current_hours: int = Field(..., ge=0)
