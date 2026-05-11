"""Fix machine name encoding in the database (eisbÃ¤r -> eisbär)"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.database import get_db_session
from sqlalchemy import text

async def fix():
    async with get_db_session() as db:
        await db.execute(text(
            "UPDATE machines SET name = 'eisbär Dehumidifier DAS-E8K.2' "
            "WHERE machine_id = 'DEHUMIDIFIER-L3'"
        ))
        await db.execute(text(
            "UPDATE machines SET description = 'DAS Dry Air System dehumidifier - "
            "supplies dry process air to Contiform mould area to prevent condensation' "
            "WHERE machine_id = 'DEHUMIDIFIER-L3'"
        ))
        print("Fixed: eisbär Dehumidifier DAS-E8K.2")

asyncio.run(fix())
