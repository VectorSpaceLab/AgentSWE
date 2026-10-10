import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--raw", required=True)
args = parser.parse_args()
data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
values = {row["method"]: float(row["value"]) for row in data["rows"]}
print(json.dumps({"metric": data["metric"], "means": values, "difference": values["cedar"] - values["baseline"]}, sort_keys=True))
