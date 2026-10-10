#!/usr/bin/env python3
"""OpenHands formal Result/Code finalizer; no deterministic semantic score."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from semantic_finalize import finalize


if __name__ == "__main__":
    raise SystemExit(finalize(["--layout", "openhands", "--code-rubric", str(ROOT / "code_axis" / "code_rubric.json"), *sys.argv[1:]]))
