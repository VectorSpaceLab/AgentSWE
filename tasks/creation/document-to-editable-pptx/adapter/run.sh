#!/usr/bin/env bash
set -euo pipefail

root=$(dirname "$0")
if [[ "${1:-}" == "one-stop" || "${1:-}" == "--one-stop" ]]; then
  shift
  exec python3 "$root/one_stop.py" "$@"
fi

exec python3 "$root/adapter.py" "$@"
