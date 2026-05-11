from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class PMRecordResponse(BaseModel):
    record_id: str
    machine_id: str
    machine_name: str
    interval_hours: int
    interval_label: str
    work_order: str
    technician_name: str
    status: str
    storage_target: Optional[str]
    download_url: Optional[str]
    file_name: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class DashboardStats(BaseModel):
    total_pms: int
    completed_this_month: int
    overdue_count: int
    due_soon_count: int
    machines_covered: int
    total_tasks: int


class OverdueItem(BaseModel):
    machine_id: str
    machine_name: str
    interval_hours: int
    interval_label: str
    last_completed_at: Optional[datetime]
    hours_overdue: Optional[int]
    current_machine_hours: Optional[int]


class ScheduleItem(BaseModel):
    machine_id: str
    machine_name: str
    interval_hours: int
    interval_label: str
    last_completed_at: Optional[datetime]
    predicted_next_due_hours: Optional[int]
    status: str  # ON_TRACK | DUE_SOON | OVERDUE | NEVER_DONE


class DashboardResponse(BaseModel):
    stats: DashboardStats
    overdue: list[OverdueItem]
    schedule: list[ScheduleItem]
    recent_pms: list[PMRecordResponse]


class HistoryFilterParams(BaseModel):
    machine_id: Optional[str] = None
    technician_id: Optional[str] = None
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    status: Optional[str] = None
    limit: int = 50
    offset: int = 0


class ApproveRequest(BaseModel):
    notes: Optional[str] = None
