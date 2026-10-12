#!/usr/bin/env bash
set -euo pipefail
export PYTHONNOUSERSITE=1

mkdir -p /logs/verifier
ENV_PREFIX="/opt/agentswe-trusted-runtime"
export HOME=/tmp/harbor-pptx-eval-home
mkdir -p "$HOME"

env -u PYTHONHOME -u PYTHONPATH "$ENV_PREFIX/bin/python" -I /tests/verify_score.py \
  --manifest /tests/eval_manifest.json \
  --eval-result /logs/artifacts/eval/eval_result.json \
  --harness-result /logs/artifacts/eval/harness_result.json \
  --verifier-dir /logs/verifier
