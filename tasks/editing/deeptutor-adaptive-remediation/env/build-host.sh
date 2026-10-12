#!/bin/bash
# Host-side build of deeptutor-task-env-v1 (runs on the evaluation host, no container).
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/deeptutor-task-env-v1)
# The venv lives at <env dir>/venv, as in the paper-era task-env-v1/venv
# (the trees reference @@AGENTSWE_ENVS@@/deeptutor-task-env-v1/venv/bin/python).
# Needs uv (setup installs a pinned uv); honours UV_PYTHON_INSTALL_MIRROR and PIP_INDEX_URL.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${AGENTSWE_HOME:-$PWD/.agentswe}
envdir=${1:-$home/envs/deeptutor-task-env-v1}
prefix=$envdir/venv
mkdir -p "$envdir"
export UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$home/python}
uv=${UV:-uv}
"$uv" python install --no-bin cpython-3.12.13-linux-x86_64-gnu
py=$("$uv" python find --no-project --python-preference only-managed cpython-3.12.13-linux-x86_64-gnu)
test "$(sha256sum "$(readlink -f "$py")" | cut -d' ' -f1)" = 021044895e95be79dc2f110367607e684119afbc8ce75f6f0eec94844e0acec7 \
  || { echo "unexpected CPython build at $py" >&2; exit 1; }
# The paper-era venv was made with `python3.12 -m venv` + pip; do the same so the
# console-script wrappers and venv layout match byte for byte.
"$py" -m venv "$prefix"
PIP_NO_CACHE_DIR=1 "$prefix/bin/python" -m pip install --no-deps --require-hashes --no-compile \
  --index-url "${PIP_INDEX_URL:-https://pypi.org/simple}" -r "$here/requirements.lock"
echo "built $prefix"
