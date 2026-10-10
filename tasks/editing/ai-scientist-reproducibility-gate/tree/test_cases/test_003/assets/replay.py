import json
from pathlib import Path

data = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))
print(json.dumps({"run_id":data["run_id"],"metric":"accuracy","value":data["value"]}, sort_keys=True))
