import argparse
import json
from pathlib import Path

from src.sampler import sample_counts


parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--draws", type=int, required=True)
parser.add_argument("--seeds", type=int, nargs="+", required=True)
args = parser.parse_args()
scores = json.loads(Path(args.input).read_text())
runs = []
for seed in args.seeds:
    probabilities, counts = sample_counts(scores, args.draws, seed)
    runs.append({"seed": seed, "probabilities": probabilities, "counts": counts})
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({"draws": args.draws, "runs": runs}, sort_keys=True, indent=2) + "\n")
