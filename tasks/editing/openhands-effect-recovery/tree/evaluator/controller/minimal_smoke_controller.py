#!/usr/bin/env python3
"""Retired external/synthetic smoke entry point.

This path used to accept independently supplied Candidate 1 and Candidate 2
directories and could therefore not prove a same-session Builder revision. It
is intentionally retained only as a fail-closed compatibility shim. Use
``harbor/formal_one_stop.py`` for the real lifecycle.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "deprecated: external/synthetic Candidate lifecycle is disabled; "
        "use harbor/formal_one_stop.py",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
