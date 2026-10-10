#!/usr/bin/env bash
set -euo pipefail

ENV_PREFIX="/opt/agentswe-trusted-runtime"
mkdir -p /logs/verifier
export HOME=/tmp/harbor-desktop-gui-verifier-home
mkdir -p "$HOME"

env -u PYTHONHOME -u PYTHONPATH "$ENV_PREFIX/bin/python" -I /tests/score_case.py \
  --output-dir /logs/artifacts/candidate_output \
  --manifest /tests/case_manifest.json \
  --run-evidence /logs/artifacts/run_evidence.json \
  --harness-evidence /logs/artifacts/trusted_evidence \
  --verifier-dir /logs/verifier
