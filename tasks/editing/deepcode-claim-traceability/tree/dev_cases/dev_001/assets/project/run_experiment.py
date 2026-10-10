from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.normalizer import affine_standardize


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    values = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = affine_standardize(values, **config)
    mean = sum(output) / len(output)
    variance = sum((value - mean) ** 2 for value in output) / len(output)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"seed": args.seed, "output": output, "mean": mean, "variance": variance},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
