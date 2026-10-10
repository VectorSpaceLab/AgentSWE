#!/bin/bash
# The paper-era overlay carried the cache stamp written by the task's
# evaluator/harness/prepare_environment.sh (sha256 of the a0 requirement files);
# keep it so that script does not re-resolve unpinned requirements from the network.
set -euo pipefail
: "${PREFIX:?}"
: > "$PREFIX/.aider-benchmark-a81a570960df80ea2a3ab1fefc78dcfc97e372d6ff7d25399943707fb0776ae2"
