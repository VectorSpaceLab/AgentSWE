#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
"$OPTIMIZATION_PYTHON" /tests/verify_score.py --eval-result /logs/artifacts/eval/eval_result.json --verifier-dir /logs/verifier
