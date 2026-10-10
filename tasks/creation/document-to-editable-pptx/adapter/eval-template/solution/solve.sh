#!/usr/bin/env bash
set -euo pipefail

ENV_PREFIX="/opt/agentswe-trusted-runtime"
export PYTHONNOUSERSITE=1
env -u PYTHONHOME -u PYTHONPATH "$ENV_PREFIX/bin/python" -I /solution/run_eval.py \
  --manifest /solution/eval_manifest.json \
  --case-root /active-case \
  --candidate-output /candidate-output \
  --eval-prompt /evaluator/eval_prompt.md \
  --rubric /evaluator/rubric.md \
  --harness /solution/pptx_harness.py \
  --output-dir /logs/artifacts/eval \
  --credential-file /run/secrets/agentswe.env \
  --timeout 600
