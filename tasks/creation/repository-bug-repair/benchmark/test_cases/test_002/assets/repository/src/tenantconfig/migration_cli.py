from __future__ import annotations

import argparse
import json
from pathlib import Path

from .migrate import migrate, rollback
from .repository import ConfigRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("mode", choices=["migrate", "rollback", "inspect"])
    args = parser.parse_args(argv)
    if args.mode == "migrate":
        result = {"migrated": migrate(args.db)}
    elif args.mode == "rollback":
        rollback(args.db)
        result = {"rolled_back": True}
    else:
        result = {"columns": sorted(ConfigRepository(args.db).schema_columns())}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
