#!/usr/bin/env python3
"""Provider-free CLI for exporting one terminal OpenHands readiness run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from evaluator.readiness_bundle import export  # noqa: E402
from harbor.readiness_preflight import PROFILE, load_and_verify_binding  # noqa: E402
from v2_usage_normalizers import make_broker_record_normalizers  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness-profile", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--cleanup-receipt", type=Path, required=True)
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--source-root", type=Path, default=TASK_ROOT)
    parser.add_argument("--control-root", type=Path, default=CONTROL_ROOT)
    args = parser.parse_args(argv)
    if args.readiness_profile != PROFILE:
        parser.error("unsupported readiness profile")
    binding, verification = load_and_verify_binding(
        args.binding_file,
        args.binding_sha256,
        source_root=args.source_root,
        control_root=args.control_root,
    )
    result = export(
        args.run_dir,
        args.destination,
        cleanup_receipt=args.cleanup_receipt,
        trusted_binding=binding,
        broker_normalizers=make_broker_record_normalizers("openhands"),
    )
    result.update({
        "schema_version": "agentswe-openhands-v2-export-receipt/v1",
        "task": "openhands",
        "profile": PROFILE,
        "binding_verification": verification,
        "readiness": "REVIEW_REQUIRED",
        "synthetic_fixture": False,
    })
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
