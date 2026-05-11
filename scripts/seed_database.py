#!/usr/bin/env python3
"""
Seed the database with PM Library data from pm_library.json.
Run once after first deploy or to reset data.

Usage:
    cd pm_project
    python scripts/seed_database.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.database import create_tables, get_db_session
from app.core.pm_library import seed_from_json


async def main():
    print("Creating database tables...")
    await create_tables()

    print("Seeding PM Library from pm_library.json...")
    async with get_db_session() as db:
        counts = await seed_from_json(db)

    print(f"Seed complete:")
    print(f"  Machines:  {counts['machines']}")
    print(f"  Intervals: {counts['intervals']}")
    print(f"  Tasks:     {counts['tasks']}")


if __name__ == "__main__":
    asyncio.run(main())
