#!/usr/bin/env python3
"""Formal dual-axis finalizer; semantic scores come only from shared judges."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from formal_axes import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
