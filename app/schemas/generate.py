from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    machine_id: str = Field(..., description="Machine identifier, e.g. CONTIFORM-C3-L3")
    interval_hours: int = Field(..., description="PM interval in hours, e.g. 120")
    work_order: str = Field(..., max_length=128, description="Work order number")
    technician_name: str = Field(..., max_length=128)
    technician_id: Optional[str] = Field(None, max_length=128)
    storage_target: Optional[str] = Field(
        None,
        pattern="^(azure|ftp|local)$",
        description="Override default storage target",
    )
    notes: Optional[str] = Field(None, max_length=1000)
    output_format: str = Field(
        "pdf",
        pattern="^(pdf|docx|xlsx)$",
        description="Output format",
    )


class GenerateResponse(BaseModel):
    record_id: str
    machine_id: str
    machine_name: str
    interval_hours: int
    interval_label: str
    work_order: str
    technician_name: str
    file_name: str
    file_size_bytes: int
    file_hash: str
    storage_target: str
    download_url: str
    task_count: int
    status: str
    created_at: str
