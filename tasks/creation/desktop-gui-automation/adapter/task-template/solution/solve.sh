#!/usr/bin/env bash
set -euo pipefail

env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 -I /solution/run_candidate.py \
  --manifest /solution/case_manifest.json \
  --submission /submission \
  --staged-input /candidate-input/input.md \
  --output /gui-candidate-output \
  --publish /logs/artifacts \
  --evidence /gui-trusted-evidence \
  --control /run-control \
  --stdout /logs/artifacts/candidate.stdout.log \
  --stderr /logs/artifacts/candidate.stderr.log \
  --timeout 600
