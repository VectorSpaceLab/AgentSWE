import argparse
import json
from pathlib import Path

from src.decay import column_decay


parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--config", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--seed", type=int, required=True)
args = parser.parse_args()
data = json.loads(Path(args.input).read_text())
config = json.loads(Path(args.config).read_text())
result = {"seed": args.seed, "weights": column_decay(data, config["tau_seconds"])}
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
