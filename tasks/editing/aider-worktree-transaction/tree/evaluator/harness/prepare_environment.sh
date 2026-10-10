#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: prepare_environment.sh PRISTINE_REPOSITORY CASE_UNIQUE_VENV" >&2
  exit 2
fi

repository=$(realpath "$1")
prefix=$(realpath -m "$2")
expected_suffix="/envs/aider-worktree-transaction-ledger-edit-v1"
if [[ "$prefix" != *"$expected_suffix" ]]; then
  echo "refusing non-case-unique prefix: $prefix" >&2
  exit 2
fi

mkdir -p "$prefix" "$prefix/pip-cache" "$prefix/tmp" "$prefix/pycache"
export PIP_CACHE_DIR="$prefix/pip-cache"
export TMPDIR="$prefix/tmp"
export PYTHONPYCACHEPREFIX="$prefix/pycache"
export PIP_DISABLE_PIP_VERSION_CHECK=1

host_python=${PYTHON_FOR_AIDER_BENCHMARK:-python3}
"$host_python" - <<'PY'
import sys
if not ((3, 10) <= sys.version_info[:2] < (3, 15)):
    raise SystemExit(f"Aider pin requires Python >=3.10,<3.15; got {sys.version.split()[0]}")
PY

if [[ ! -x "$prefix/bin/python" ]]; then
  "$host_python" -m venv "$prefix"
fi

stamp_input=$(sha256sum "$repository/requirements.txt" "$repository/requirements/requirements-dev.txt" "$repository/pyproject.toml")
stamp=$(printf '%s\n' "$stamp_input" | sha256sum | cut -d' ' -f1)
if [[ ! -f "$prefix/.aider-benchmark-$stamp" ]]; then
  "$prefix/bin/python" -m pip install \
    -r "$repository/requirements.txt" \
    -r "$repository/requirements/requirements-dev.txt"
  "$prefix/bin/python" -m pip install --no-deps -e "$repository"
  rm -f "$prefix"/.aider-benchmark-*
  : > "$prefix/.aider-benchmark-$stamp"
fi

"$prefix/bin/python" -c 'import pytest; import aider; print("prepared", pytest.__version__)'
