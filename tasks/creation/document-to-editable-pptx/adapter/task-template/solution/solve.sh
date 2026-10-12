#!/usr/bin/env bash
set -euo pipefail

ENV_PREFIX="/opt/agentswe/benchmark/agent-create-0804/envs/document-to-editable-pptx-agent-v2"
export PYTHONNOUSERSITE=1
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 -I /solution/run_candidate.py \
  --manifest /solution/case_manifest.json \
  --submission /submission \
  --cases-root /active-case \
  --output /logs/artifacts/candidate_output \
  --evidence /logs/artifacts/run_evidence.json \
  --stdout /logs/artifacts/candidate.stdout.log \
  --stderr /logs/artifacts/candidate.stderr.log \
  --timeout 600
