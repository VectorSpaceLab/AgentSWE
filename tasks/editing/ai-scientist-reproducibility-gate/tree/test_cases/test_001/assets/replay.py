import json
from pathlib import Path

data = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))
print(json.dumps({row["experiment_id"]: row["value"] for row in data["rows"]}, sort_keys=True))
