#!/usr/bin/env python3
"""
Generate all PM checklists for all machines and intervals.
Outputs PDFs to ./output/pm-docs/

Usage:
    cd pm_project
    python scripts/generate_all_pms.py [--format pdf|docx|xlsx]
"""
import asyncio
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.database import create_tables, get_db_session
from app.db.crud import get_all_machines, get_intervals_for_machine, get_tasks_for_interval, get_machine
from app.core.document_generator import PMDocument, generate_pdf, generate_docx, generate_xlsx
from app.core.pm_library import extract_parts_from_tasks, seed_from_json
from app.utils.security import compute_file_hash


async def main(fmt: str = "pdf"):
    await create_tables()

    async with get_db_session() as db:
        machines = await get_all_machines(db)
        if not machines:
            print("No machines found — seeding library first...")
            counts = await seed_from_json(db)
            print(f"Seeded: {counts}")
            machines = await get_all_machines(db)

        total = 0
        for machine in machines:
            intervals = await get_intervals_for_machine(db, machine.machine_id)
            print(f"\n{machine.name} ({machine.machine_id})")
            for interval in intervals:
                tasks = await get_tasks_for_interval(db, machine.machine_id, interval.hours)
                if not tasks:
                    print(f"  {interval.label}: no tasks — skipping")
                    continue

                now = datetime.utcnow()
                file_name = f"PM_{machine.machine_id}_{interval.hours}hr_{now.strftime('%Y%m%d')}.{fmt}"
                out_dir = Path("output") / "pm-docs" / machine.machine_id / str(now.year) / f"{now.month:02d}"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / file_name

                parts = extract_parts_from_tasks(tasks)
                pm_doc = PMDocument(
                    machine_name=machine.name,
                    machine_id=machine.machine_id,
                    interval_hours=interval.hours,
                    interval_label=interval.label,
                    work_order=f"SEED-{now.strftime('%Y%m%d')}",
                    technician_name="System Generated",
                    tasks=tasks,
                    parts=parts,
                    generated_at=now,
                )

                if fmt == "pdf":
                    generate_pdf(pm_doc, out_path)
                elif fmt == "docx":
                    generate_docx(pm_doc, out_path)
                else:
                    generate_xlsx(pm_doc, out_path)

                file_hash = compute_file_hash(out_path)
                size_kb = out_path.stat().st_size / 1024
                print(f"  OK {interval.label}: {len(tasks)} tasks -> {out_path.name} ({size_kb:.1f}KB)")
                total += 1

        print(f"\nGenerated {total} PM documents to ./output/pm-docs/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate all PM checklists")
    parser.add_argument("--format", choices=["pdf", "docx", "xlsx"], default="pdf")
    args = parser.parse_args()
    asyncio.run(main(args.format))
