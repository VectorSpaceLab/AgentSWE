#!/bin/bash
# Host-side build of claude-policy-python311: the CPython 3.11 the Claude evaluator runs its syntax check with
# (agentloop/evaluator/materialize.py: `python -m py_compile` of the Candidate's hook and inspector, on the host).
# The paper used whatever Python >= 3.11 the evaluation host offered; this pins one.
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/claude-policy-python311)
# Needs uv (setup installs a pinned uv); honours UV_PYTHON_INSTALL_MIRROR.
set -euo pipefail
home=${AGENTSWE_HOME:-$PWD/.agentswe}
envdir=${1:-$home/envs/claude-policy-python311}
export UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$home/python}
uv=${UV:-uv}
build=cpython-3.11.15-linux-x86_64-gnu
"$uv" python install --no-bin "$build"
py=$(readlink -f "$("$uv" python find --no-project --python-preference only-managed "$build")")
test "$(sha256sum "$py" | cut -d' ' -f1)" = 0253b01a7f5d96a042a2b9f5e0f5ff5062707a1c75be2b089098e3111cd186d2 \
  || { echo "unexpected CPython build at $py" >&2; exit 1; }
mkdir -p "$envdir/bin"
ln -sfn "$py" "$envdir/bin/python3.11"
"$envdir/bin/python3.11" -c 'import sys, json, pathlib, hashlib; assert sys.version_info[:2] == (3, 11)'
date -u +%FT%TZ > "$envdir/.agentswe-built"   # setup's ready_file marker: written last
echo "built $envdir"
