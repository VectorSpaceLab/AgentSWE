#!/usr/bin/env python3
"""Formal dual-axis finalizer; semantic scores come only from shared judges."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from formal_axes import main  # noqa: E402

def code_contract(_path: Path, _run: Path):
    return "N/A", False, ["legacy/static Code contracts cannot publish; invoke Create code_eval.py"]

if __name__ == "__main__":
    raise SystemExit(main())
