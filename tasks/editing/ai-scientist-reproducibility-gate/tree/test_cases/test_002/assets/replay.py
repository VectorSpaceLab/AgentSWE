import json
from pathlib import Path

rows = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))["rows"]
diffs = [row["birch"] - row["baseline"] for row in rows]
print(json.dumps({"seed_count":len(diffs),"mean_difference":sum(diffs)/len(diffs),"negative_seeds":[rows[i]["seed"] for i,d in enumerate(diffs) if d < 0]}, sort_keys=True))
