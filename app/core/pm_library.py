from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import crud
from app.db.models import Machine, PMInterval, Task

logger = logging.getLogger(__name__)
settings = get_settings()


async def seed_from_json(db: AsyncSession, path: Path | None = None) -> dict[str, int]:
    """Load pm_library.json into the database. Skips existing records (idempotent)."""
    path = path or settings.pm_library_path
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)

    counts: dict[str, int] = {"machines": 0, "intervals": 0, "tasks": 0}

    for m in data.get("machines", []):
        existing = await crud.get_machine(db, m["machine_id"])
        if not existing:
            chapters = m.get("maintenance_chapters", [])
            await crud.create_machine(
                db,
                {
                    "machine_id": m["machine_id"],
                    "name": m["name"],
                    "manufacturer": m["manufacturer"],
                    "model": m["model"],
                    "machine_type": m.get("type", "THIRD_PARTY"),
                    "maintenance_chapters": json.dumps(chapters),
                    "is_hour_based": m.get("is_hour_based", True),
                    "description": m.get("description"),
                    "location": m.get("location"),
                    "asset_tag": m.get("asset_tag"),
                },
            )
            counts["machines"] += 1

    for iv in data.get("intervals", []):
        existing_ivs = await crud.get_intervals_for_machine(db, iv["machine_id"])
        if not any(e.hours == iv["hours"] for e in existing_ivs):
            await crud.create_interval(
                db,
                {
                    "machine_id": iv["machine_id"],
                    "hours": iv["hours"],
                    "label": iv["label"],
                    "natural_label": iv["natural_label"],
                    "source": iv.get("source"),
                },
            )
            counts["intervals"] += 1

    for t in data.get("tasks", []):
        existing = await crud.get_tasks_for_interval(db, t["machine_id"], t["interval_hours"])
        if not any(e.task_no == t["task_no"] for e in existing):
            import uuid
            await crud.create_task(
                db,
                {
                    "task_id": str(uuid.uuid4()),
                    "machine_id": t["machine_id"],
                    "interval_hours": t["interval_hours"],
                    "task_no": t["task_no"],
                    "area": t["area"],
                    "action": t["action"],
                    "description": t["description"],
                    "machine_state": t["machine_state"],
                    "safety_flag": t.get("safety_flag", False),
                    "part_number": t.get("part_number"),
                    "source_chapter": t.get("source_chapter"),
                    "source_section": t.get("source_section"),
                },
            )
            counts["tasks"] += 1

    logger.info("PM Library seed complete: %s", counts)
    return counts


async def get_interval_label(db: AsyncSession, machine_id: str, hours: int) -> str:
    intervals = await crud.get_intervals_for_machine(db, machine_id)
    for iv in intervals:
        if iv.hours == hours:
            return iv.label
    return f"{hours}hr"


def extract_parts_from_tasks(tasks: list[Task]) -> list[dict]:
    seen: set[str] = set()
    parts = []
    for t in tasks:
        if t.part_number and t.part_number not in seen:
            seen.add(t.part_number)
            parts.append(
                {
                    "part_number": t.part_number,
                    "description": _part_description(t.part_number),
                }
            )
    return parts


_KNOWN_PARTS: dict[str, str] = {
    "L011362": "BAG Filter Elements — Fume Extractor",
    "L011378": "BOX Filter Elements — Fume Extractor",
    "KRONES-ACF-001": "Activated Carbon Filter Element — Contiform Mould Area",
    "KRONES-HPF-001": "High-Pressure Blow Air Filter Element",
    "EISBAR-F-MAIN-001": "Main Process Air Filter Element — DAS-E8K.2",
    "EISBAR-F-PRE-001": "Pre-Filter Element — DAS-E8K.2 Inlet",
    "VAR-FFILM-BELT-001": "Film Feed Belt — Variopac Pro FS",
    "VAR-GEARBOX-OIL-001": "Krones Gearbox Oil — Variopac Main Drive",
}


def _part_description(part_number: str) -> str:
    return _KNOWN_PARTS.get(part_number, "Replacement Part")
