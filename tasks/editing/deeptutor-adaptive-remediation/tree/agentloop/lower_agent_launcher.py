#!/usr/bin/env python3
"""Evaluator-owned isolated launcher for the actual DeepTutor lower agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .lower_agent_entry import run
except ImportError:  # pragma: no cover
    from lower_agent_entry import run  # type: ignore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--source-repository",type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--python", dest="python_executable")
    args = parser.parse_args()
    result = run(
        args.repository,
        args.prompt,
        args.output,
        broker_endpoint=args.broker_endpoint,
        execute=args.execute,
        python_executable=args.python_executable,
        source_repository=args.source_repository,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if not args.execute or result.get("exit_code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
