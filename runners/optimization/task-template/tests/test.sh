#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
"$OPTIMIZATION_PYTHON" /tests/verify_candidate.py --case-id "$CASE_ID" --candidate-evidence /logs/artifacts/candidate_run/candidate_evidence.json --resource-evidence /broker-evidence/resource_evidence.json --predictions /logs/artifacts/candidate_output/predictions.jsonl --verifier-dir /logs/verifier --candidate-digest "$CANDIDATE_DIGEST"
