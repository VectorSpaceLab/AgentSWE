from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.selector import select_first_max


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    candidates = json.loads(Path(args.input).read_text(encoding="utf-8"))
    selected, tied = select_first_max(candidates)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "selected": selected,
                "tie": tied,
                "input_order": [item["id"] for item in candidates],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
