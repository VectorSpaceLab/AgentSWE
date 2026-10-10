#!/usr/bin/env bash
set -euo pipefail

ENV_PREFIX="/opt/agentswe-trusted-runtime"
mkdir -p /logs/verifier
export HOME=/tmp/harbor-formal-verifier-home
mkdir -p "$HOME"

env -u PYTHONHOME -u PYTHONPATH "$ENV_PREFIX/bin/python" -I /tests/score_case.py \
  --cases-root /active-case \
  --output-dir /logs/artifacts/candidate_output \
  --manifest /tests/case_manifest.json \
  --harness /tests/evaluate_case.py \
  --run-evidence /logs/artifacts/run_evidence.json \
  --verifier-dir /logs/verifier
