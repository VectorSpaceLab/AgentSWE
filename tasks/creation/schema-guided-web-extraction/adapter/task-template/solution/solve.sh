#!/usr/bin/env bash
set -euo pipefail

ENV_PREFIX="/opt/agentswe/benchmark/envs/schema-guided-web-extraction-agent-hard-v2"
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 -I /solution/run_candidate.py \
  --manifest /solution/case_manifest.json \
  --submission /submission \
  --cases-root /active-case \
  --output /logs/artifacts/candidate_output \
  --evidence /logs/artifacts/run_evidence.json \
  --stdout /logs/artifacts/candidate.stdout.log \
  --stderr /logs/artifacts/candidate.stderr.log \
  --timeout 600
