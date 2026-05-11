from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class MachineCreate(BaseModel):
    machine_id: str = Field(..., max_length=64, pattern=r"^[A-Z0-9_\-]+$")
    name: str = Field(..., max_length=128)
    manufacturer: str = Field(..., max_length=128)
    model: str = Field(..., max_length=128)
    machine_type: str = Field(..., pattern="^(KRONES|THIRD_PARTY)$")
    maintenance_chapters: Optional[list[int]] = None
    is_hour_based: bool = True
    description: Optional[str] = None
    location: Optional[str] = Field(None, max_length=128)
    asset_tag: Optional[str] = Field(None, max_length=64)


class MachineUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = None
    location: Optional[str] = Field(None, max_length=128)
    asset_tag: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = None


class MachineResponse(BaseModel):
    machine_id: str
    name: str
    manufacturer: str
    model: str
    machine_type: str
    is_hour_based: bool
    description: Optional[str]
    location: Optional[str]
    asset_tag: Optional[str]
    is_active: bool

    model_config = {"from_attributes": True}


class MachineHoursResponse(BaseModel):
    machine_id: str
    current_hours: int
    recorded_at: str
    recorded_by: Optional[str]
