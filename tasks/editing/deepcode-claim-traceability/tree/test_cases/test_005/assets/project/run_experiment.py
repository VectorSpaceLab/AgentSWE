import argparse
import json
from pathlib import Path

from src.model import evaluate


parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--full", required=True)
parser.add_argument("--ablation", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--seed", type=int, required=True)
args = parser.parse_args()
values = json.loads(Path(args.input).read_text())
full_config = json.loads(Path(args.full).read_text())
ablation_config = json.loads(Path(args.ablation).read_text())
full_outputs, full_mean = evaluate(values, full_config["gate_enabled"])
ablated_outputs, ablated_mean = evaluate(values, ablation_config["gate_enabled"])
result = {"seed": args.seed, "full": {"outputs": full_outputs, "mean": full_mean}, "without_gate": {"outputs": ablated_outputs, "mean": ablated_mean}, "improvement": full_mean - ablated_mean}
target = Path(args.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
