#!/usr/bin/env bash
set -euo pipefail
"$OPTIMIZATION_PYTHON" /solution/run_candidate.py --submission /submission --case /active-case/$CASE_ID --output /logs/artifacts/candidate_output --run-dir /logs/artifacts/candidate_run --case-id "$CASE_ID" --timeout 900
