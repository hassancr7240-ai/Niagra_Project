import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

lib_path = Path("data/pm_library.json")
with open(lib_path, encoding="utf-8-sig") as f:
    data = json.load(f)

for m in data["machines"]:
    if m["machine_id"] == "DEHUMIDIFIER-L3":
        m["name"] = "eisbär Dehumidifier DAS-E8K.2"
        m["manufacturer"] = "eisbär Trockentechnik GmbH"
        m["description"] = "DAS Dry Air System dehumidifier - supplies dry process air to Contiform mould area"

with open(lib_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=True, indent=2)
print("Fixed pm_library.json with ASCII escapes")

db_path = Path("data/pm_automation.db")
if db_path.exists():
    db_path.unlink()
    print("Deleted old SQLite database")

async def reseed():
    from app.db.database import create_tables, get_db_session
    from app.core.pm_library import seed_from_json
    await create_tables()
    async with get_db_session() as db:
        counts = await seed_from_json(db)
    return counts

counts = asyncio.run(reseed())
print("Re-seeded:", counts)
print("Done - restart server")
