#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
package=$(cd "$here/../.." && pwd)
python=${AIDER_BENCH_PYTHON:-/opt/agentswe/benchmark/envs/aider-worktree-transaction-ledger-edit-v1/bin/python}
source=${AIDER_BENCH_SOURCE:-$package/input/repository}
output=${1:-$here/dev-output}
exec "$python" "$package/dev_cases/run_dev_case.py" --case-dir "$here" --python "$python" --source "$source" --output-dir "$output"
