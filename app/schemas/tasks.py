from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class TaskBase(BaseModel):
    task_no: int = Field(..., ge=10, description="Task number (10, 20, 30 ... steps of 10)")
    area: str = Field(..., max_length=64)
    action: str = Field(..., max_length=64)
    description: str
    machine_state: str = Field(..., pattern="^(RUNNING|STOPPED|POWERED_OFF)$")
    safety_flag: bool = False
    part_number: Optional[str] = Field(None, max_length=64)
    source_chapter: Optional[str] = Field(None, max_length=64)
    source_section: Optional[str] = Field(None, max_length=64)


class TaskCreate(TaskBase):
    machine_id: str
    interval_hours: int


class TaskResponse(TaskBase):
    task_id: str
    machine_id: str
    interval_hours: int
    is_active: bool

    model_config = {"from_attributes": True}


class TaskSchema(BaseModel):
    task_no: int
    area: str
    action: str
    description: str
    machine_state: str
    safety_flag: bool
    part_number: Optional[str] = None
    source_chapter: Optional[str] = None
    source_section: Optional[str] = None

    model_config = {"from_attributes": True}
