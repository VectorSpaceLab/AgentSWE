from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import SessionArchive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--mode", choices=["migrate", "inspect"], default="migrate")
    args = parser.parse_args(argv)
    archive = SessionArchive(args.archive)
    if args.mode == "inspect":
        print(json.dumps(archive.load_all(), sort_keys=True))
    else:
        print(json.dumps({"migrated": archive.migrate_file()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
