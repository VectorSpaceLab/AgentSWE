import argparse
import json
from pathlib import Path

from src.update import decay


parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--seed", type=int, required=True)
args = parser.parse_args()
config = json.loads(Path(args.config).read_text())
final = decay(config["initial_value"], config["learning_rate"], config["steps"])
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({"seed": args.seed, "final": final, **config}, sort_keys=True, indent=2) + "\n")
