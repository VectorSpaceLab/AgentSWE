import argparse
import csv
import json
from pathlib import Path

from src.pipeline import split_and_standardize


parser = argparse.ArgumentParser()
parser.add_argument("--data", required=True)
parser.add_argument("--config", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--seed", type=int, required=True)
args = parser.parse_args()
with Path(args.data).open(newline="") as handle:
    values = [float(row["value"]) for row in csv.DictReader(handle)]
config = json.loads(Path(args.config).read_text())
mean, std, train, test = split_and_standardize(values, config["train_count"])
payload = {"seed": args.seed, "train_indices": list(range(6)), "test_indices": [6, 7], "train_mean": mean, "train_std": std, "train": train, "test": test}
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
