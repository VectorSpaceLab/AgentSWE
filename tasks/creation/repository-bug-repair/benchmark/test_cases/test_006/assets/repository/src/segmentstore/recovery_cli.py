from __future__ import annotations

import argparse
import json
from pathlib import Path

from .inspect import inspect_layout
from .store import SegmentStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("mode", choices=["recover", "inspect"])
    args = parser.parse_args(argv)
    if args.mode == "recover":
        result = {"outcome": SegmentStore(args.root).recover()}
    else:
        result = inspect_layout(args.root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
