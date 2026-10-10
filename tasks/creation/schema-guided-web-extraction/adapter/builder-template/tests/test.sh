#!/usr/bin/env bash
set -euo pipefail

mkdir -p /logs/verifier
python3 /tests/verify_builder.py \
  --submission /workspace/submission \
  --verifier-dir /logs/verifier
