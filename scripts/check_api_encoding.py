import json
with open("api_response.txt", encoding="utf-8-sig") as f:
    data = json.load(f)
for m in data:
    if "DEHUMIDIFIER" in m["machine_id"]:
        print("name:", m["name"])
        print("mfr:", m["manufacturer"])
        print("ascii ok:", all(ord(c) < 128 for c in m["name"]))
        print("contains eisbar:", "eisb" in m["name"].lower())
