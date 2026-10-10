#!/bin/bash
# Host-side build of deepcode-runtime-venv (runs on the evaluation host, no container).
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/deepcode-runtime-venv)
# The env dir IS the virtualenv: the control plane runs @@AGENTSWE_ENVS@@/deepcode-runtime-venv/bin/python,
# and the lower agent finds the prefix to bind through bin/../pyvenv.cfg.
# Needs uv (setup installs a pinned uv); honours UV_PYTHON_INSTALL_MIRROR and PIP_INDEX_URL.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${AGENTSWE_HOME:?AGENTSWE_HOME must be set}
prefix=${1:-$home/envs/deepcode-runtime-venv}
marker=$prefix/.agentswe-env-ready
want=$(sha256sum "$here/requirements.lock" | cut -d' ' -f1)
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$want" ]; then
  echo "up to date: $prefix"; exit 0
fi
case "$prefix" in "$home"/envs/*) ;; *) echo "refusing to build outside $home/envs: $prefix" >&2; exit 1;; esac
rm -rf "$prefix"   # a partial or stale build; the marker is written only after a complete one
export UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$home/python}
uv=${UV:-uv}
"$uv" python install --no-bin cpython-3.12.13-linux-x86_64-gnu
py=$("$uv" python find --no-project --python-preference only-managed cpython-3.12.13-linux-x86_64-gnu)
test "$(sha256sum "$(readlink -f "$py")" | cut -d' ' -f1)" = 021044895e95be79dc2f110367607e684119afbc8ce75f6f0eec94844e0acec7 \
  || { echo "unexpected CPython build at $py" >&2; exit 1; }
# The paper venv was made with `python3.12 -m venv` + pip; do the same so the layout matches.
"$py" -m venv "$prefix"
PIP_NO_CACHE_DIR=1 "$prefix/bin/python" -m pip install --no-deps --require-hashes --no-compile \
  --index-url "${PIP_INDEX_URL:-https://pypi.org/simple}" -r "$here/requirements.lock"
# The imports formal_one_stop._runtime_has_product_dependencies requires before any lower product run.
"$prefix/bin/python" -c "import aiohttp,httpx,loguru,openai,pydantic_settings,yaml,rich"
echo "$want" > "$marker"
echo "built $prefix"
