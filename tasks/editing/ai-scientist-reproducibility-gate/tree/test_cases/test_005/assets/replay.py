import json
import statistics
from pathlib import Path

rows = json.loads(Path("raw_results.json").read_text(encoding="utf-8"))["rows"]
diffs = [row["ember"] - row["baseline"] for row in rows]
print(json.dumps({"mean_difference":sum(diffs)/len(diffs),"standard_deviation_difference":statistics.stdev(diffs),"seed_count":len(diffs)}, sort_keys=True))
