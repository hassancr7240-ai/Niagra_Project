"""Fix eisbär encoding directly in SQLite database."""
import sqlite3
import sys
from pathlib import Path

db_path = Path(__file__).parent.parent / "data" / "pm_automation.db"
conn = sqlite3.connect(str(db_path), check_same_thread=False)

# Check current
cur = conn.execute("SELECT name FROM machines WHERE machine_id = 'DEHUMIDIFIER-L3'")
current = cur.fetchone()[0]
print(f"Current: {repr(current)}")

# Write correct UTF-8 value
correct_name = "eisbär Dehumidifier DAS-E8K.2"
correct_mfr  = "eisbär Trockentechnik GmbH"
correct_desc = "DAS Dry Air System dehumidifier - supplies dry process air to Contiform mould area to prevent condensation"

conn.execute(
    "UPDATE machines SET name=?, manufacturer=?, description=? WHERE machine_id=?",
    (correct_name, correct_mfr, correct_desc, "DEHUMIDIFIER-L3")
)
conn.commit()

cur2 = conn.execute("SELECT name, manufacturer FROM machines WHERE machine_id='DEHUMIDIFIER-L3'")
fixed = cur2.fetchone()
print(f"Fixed name: {repr(fixed[0])}")
print(f"Fixed mfr:  {repr(fixed[1])}")

# Write to file to verify
out = Path(__file__).parent.parent / "check_name.txt"
out.write_text(f"name={fixed[0]}\nmfr={fixed[1]}\n", encoding="utf-8")
conn.close()
print("Done. Check check_name.txt")
