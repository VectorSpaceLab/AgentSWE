import json
from pathlib import Path

data = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))
print(json.dumps({"experiment_id":data["experiment_id"],"metric":data["metric"],"value":data["value"]}, sort_keys=True))
