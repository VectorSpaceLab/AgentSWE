#!/usr/bin/env bash
set -euo pipefail
"$OPTIMIZATION_PYTHON" /solution/run_eval.py \
  --benchmark-id "$BENCHMARK_ID" \
  --case-id "$CASE_ID" \
  --predictions /candidate-output/predictions.jsonl \
  --gold /evaluator/gold.json \
  --question "/active-case/$CASE_ID/input.md" \
  --credential-file "${HARNESS_CREDENTIAL_FILE:-/run/secrets/agentswe.env}" \
  --cache "/logs/artifacts/eval/judge_cache.json" \
  --output /logs/artifacts/eval
