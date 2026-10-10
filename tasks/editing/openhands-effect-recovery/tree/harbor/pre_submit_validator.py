"""Fail-closed task-local pre-submit adapter for OpenHands readiness.

The adapter is safe to mount into a Builder.  It only reads the proposed
delivery, never evaluates a model request, and emits no shared control-plane
state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor.readiness_contract import FILES, validate_submission


def validate(path: Path, *, number: int = 1, previous_candidate_digest: str | None = None,
             previous_feedback_digest: str | None = None) -> dict:
    errors = validate_submission(path, number=number,
                                 previous_candidate_digest=previous_candidate_digest,
                                 previous_feedback_digest=previous_feedback_digest)
    return {
        "schema_version": "agentswe-openhands-pre-submit/v1",
        "submission": str(Path(path).resolve()),
        "profile": "single-dev-two-round-hidden-smoke-v1",
        "required_files": list(FILES),
        "submission_number": number,
        "errors": errors,
        "valid": not errors,
        "provider_calls": 0,
        "shared_registry_written": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("submission", type=Path)
    parser.add_argument("--number", type=int, default=1)
    parser.add_argument("--previous-candidate-digest")
    parser.add_argument("--previous-feedback-digest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = validate(args.submission, number=args.number,
                      previous_candidate_digest=args.previous_candidate_digest,
                      previous_feedback_digest=args.previous_feedback_digest)
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
