import json

with open('data/pm_library.json', encoding='utf-8-sig') as f:
    data = json.load(f)

shrink_machine = {
    "machine_id": "SHRINK-TUNNEL-L3",
    "name": "Krones Shrinking Tunnel",
    "manufacturer": "Krones AG",
    "model": "Variopac Shrinking Tunnel",
    "type": "KRONES",
    "maintenance_chapters": [12],
    "is_hour_based": True,
    "description": "Shrinking tunnel - heat-shrinks film tightly around wrapped packs from Variopac. Separate manual pending - onboard via RAG pipeline when manual provided.",
    "location": "Line 3",
    "asset_tag": "SHT-L3"
}

existing_ids = [m["machine_id"] for m in data["machines"]]
if "SHRINK-TUNNEL-L3" not in existing_ids:
    data["machines"].append(shrink_machine)
    print("Added SHRINK-TUNNEL-L3 as 5th machine")

machines = len(data["machines"])
tasks = len(data["tasks"])
intervals = len(data["intervals"])
print(f"Machines: {machines}, Tasks: {tasks}, Intervals: {intervals}")

with open('data/pm_library.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("Saved.")
