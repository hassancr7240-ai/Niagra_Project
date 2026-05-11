from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, status

from app.db import crud
from app.dependencies import CurrentUserDep, DBDep
from app.schemas.machines import MachineCreate, MachineResponse, MachineUpdate
from app.utils.audit import log_action

router = APIRouter(prefix="/api/machines", tags=["Machines"])


@router.get("", response_model=list[MachineResponse])
async def list_machines(user: CurrentUserDep, db: DBDep) -> list[MachineResponse]:
    user.require("machines:read")
    machines = await crud.get_all_machines(db)
    return [MachineResponse.model_validate(m) for m in machines]


@router.get("/{machine_id}", response_model=MachineResponse)
async def get_machine(machine_id: str, user: CurrentUserDep, db: DBDep) -> MachineResponse:
    user.require("machines:read")
    machine = await crud.get_machine(db, machine_id)
    if not machine:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")
    return MachineResponse.model_validate(machine)


@router.post("", response_model=MachineResponse, status_code=status.HTTP_201_CREATED)
async def create_machine(
    body: MachineCreate, user: CurrentUserDep, db: DBDep
) -> MachineResponse:
    user.require("machines:write")

    existing = await crud.get_machine(db, body.machine_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Machine '{body.machine_id}' already exists",
        )

    machine = await crud.create_machine(
        db,
        {
            "machine_id": body.machine_id,
            "name": body.name,
            "manufacturer": body.manufacturer,
            "model": body.model,
            "machine_type": body.machine_type,
            "maintenance_chapters": json.dumps(body.maintenance_chapters or []),
            "is_hour_based": body.is_hour_based,
            "description": body.description,
            "location": body.location,
            "asset_tag": body.asset_tag,
        },
    )

    await log_action(
        db,
        action="MACHINE_CREATED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="machine",
        resource_id=body.machine_id,
        details={"name": body.name, "manufacturer": body.manufacturer},
        ip_address=user.ip_address,
    )

    return MachineResponse.model_validate(machine)


@router.patch("/{machine_id}", response_model=MachineResponse)
async def update_machine(
    machine_id: str, body: MachineUpdate, user: CurrentUserDep, db: DBDep
) -> MachineResponse:
    user.require("machines:write")

    machine = await crud.get_machine(db, machine_id)
    if not machine:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    updates = body.model_dump(exclude_none=True)
    updated = await crud.update_machine(db, machine_id, updates)

    await log_action(
        db,
        action="MACHINE_UPDATED",
        user_id=user.user_id,
        user_email=user.email,
        resource_type="machine",
        resource_id=machine_id,
        details=updates,
        ip_address=user.ip_address,
    )

    return MachineResponse.model_validate(updated)
