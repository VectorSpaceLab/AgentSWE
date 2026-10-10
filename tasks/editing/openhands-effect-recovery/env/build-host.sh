#!/bin/bash
# Host-side build of openhands-effect-recovery-ledger-edit-v1: the prewarmed OpenHands frontend dependencies.
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/openhands-effect-recovery-ledger-edit-v1)
#
# What the evaluator reads (harbor/builder_dependencies.py, lower_agent/isolated_runtime.py, formal_one_stop.py):
#   baseline-install/{package.json,package-lock.json}  identical to a0's (checked against the public package each run)
#   baseline-install/.benchmark-lock-sha256            sha256 of that package-lock.json
#   baseline-install/node_modules                      `npm ci --ignore-scripts` of that lock (vitest, react-router, tsc ...)
#   npm-tooling/node_modules/npm                       npm 10.5.0, run with the host's node (>= 22.12)
# Recipe: npm 10.5.0 from the registry tarball (sha256-pinned), then `npm ci --ignore-scripts` of a0's lock with it;
# npm checks every package against the lock's sha512 integrity. Rebuilt this way, node_modules is byte-identical to the
# paper environment's (all files except one vitest results cache the paper's runs left behind).
set -euo pipefail
home=${AGENTSWE_HOME:-$PWD/.agentswe}
name=openhands-effect-recovery-ledger-edit-v1
envdir=${1:-$home/envs/$name}
registry=${AGENTSWE_NPM_REGISTRY:-https://registry.npmjs.org}
registry=${registry%/}
a0=$home/editing/tasks/openhands-effect-recovery/tree/input/repository
npm_tgz_sha256=17ca6e08e7633b624e8f870db81a78f46afe119de62bcaf0a7407574139198fc
lock_sha256=eb21ed2a5f4e06d1584d4650913f00f457b69bc97ba175e855406bf96b6c8711
package_sha256=08757b61c4fedc4e55e2504c07d15de205e5452f1bd560c4da9106b3d248f45d

if [ -e "$envdir/baseline-install" ] || [ -e "$envdir/npm-tooling" ]; then
  echo "$envdir exists but is incomplete; remove it and rerun setup" >&2
  exit 1
fi
for f in package.json package-lock.json; do
  [ -f "$a0/$f" ] || { echo "a0 $f not found under $a0 (setup fetches a0 before building environments)" >&2; exit 1; }
done
[ "$(sha256sum "$a0/package-lock.json" | cut -d' ' -f1)" = "$lock_sha256" ] || { echo "a0 package-lock.json is not the pinned one" >&2; exit 1; }
[ "$(sha256sum "$a0/package.json" | cut -d' ' -f1)" = "$package_sha256" ] || { echo "a0 package.json is not the pinned one" >&2; exit 1; }
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=12)?0:1)' \
  || { echo "host node >= 22.12 is required" >&2; exit 1; }

tmp=$(mktemp -d "$home/cache/$name.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$envdir/npm-tooling/node_modules" "$envdir/baseline-install" "$envdir/npm-cache"

# 1. npm 10.5.0
curl -fL --retry 5 --retry-all-errors --connect-timeout 30 -sS -o "$tmp/npm-10.5.0.tgz" "$registry/npm/-/npm-10.5.0.tgz"
[ "$(sha256sum "$tmp/npm-10.5.0.tgz" | cut -d' ' -f1)" = "$npm_tgz_sha256" ] || { echo "npm-10.5.0.tgz sha256 mismatch" >&2; exit 1; }
tar -xzf "$tmp/npm-10.5.0.tgz" -C "$tmp"
mv "$tmp/package" "$envdir/npm-tooling/node_modules/npm"
npm_cli=$envdir/npm-tooling/node_modules/npm/bin/npm-cli.js
test "$(node "$npm_cli" --version)" = 10.5.0

# 2. the prewarmed install of a0's exact lock
cp "$a0/package.json" "$a0/package-lock.json" "$envdir/baseline-install/"
(cd "$envdir/baseline-install" && HOME="$tmp/home" npm_config_cache="$tmp/npm-cache" \
  node "$npm_cli" ci --ignore-scripts --no-audit --no-fund --registry="$registry/")
printf '%s\n' "$lock_sha256" > "$envdir/baseline-install/.benchmark-lock-sha256"
test -x "$envdir/baseline-install/node_modules/.bin/vitest"
date -u +%FT%TZ > "$envdir/.agentswe-built"   # setup's ready_file marker: written last
echo "built $envdir"
