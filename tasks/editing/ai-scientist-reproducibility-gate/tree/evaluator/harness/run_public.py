#!/usr/bin/env python3
from pathlib import Path
import sys


PUBLIC_HARNESS_DIR = Path(__file__).resolve().parents[2] / "dev_cases"
sys.path.insert(0, str(PUBLIC_HARNESS_DIR))

from public_runner import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
