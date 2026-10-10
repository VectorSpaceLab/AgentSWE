import json
from pathlib import Path

data = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))
print(json.dumps(data["results"], sort_keys=True))
