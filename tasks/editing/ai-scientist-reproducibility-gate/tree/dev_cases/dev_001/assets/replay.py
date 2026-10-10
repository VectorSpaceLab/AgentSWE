import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--raw", required=True)
args = parser.parse_args()
data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
groups = {}
for row in data["rows"]:
    groups.setdefault(row["method"], []).append(float(row["value"]))
means = {name: sum(values) / len(values) for name, values in sorted(groups.items())}
baseline, candidate = means["baseline"], means["nova"]
print(json.dumps({"metric": data["metric"], "means": means, "difference": candidate - baseline}, sort_keys=True))
