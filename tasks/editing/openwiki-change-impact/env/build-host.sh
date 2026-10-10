#!/bin/bash
# Host-side build of openwiki-runtime-v1, the evaluator-owned `.runtime` of the OpenWiki agentloop tree.
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/openwiki-runtime-v1)
# setup links <rendered tree>/.runtime -> <env dir> (env.json "tree_link"); the tree reads only:
#   node-v22.12.0-linux-x64/            Node 22.12.0 (lower agent, candidate build, product start witness)
#   pnpm-store/v11/links/@/pnpm/10.33.2/<integrity>/node_modules/pnpm   pnpm 10.33.2 (candidate builds; preferred)
#   node_modules/pnpm/                  pnpm 11.15.1 (fallback the adapter also lists)
#   agentloop-bin/pnpm                  wrapper on the sandbox PATH
#   candidate-smoke/repository/node_modules   the complete offline dependency tree mounted read-only for the Builder
#                                       and materialized into every Candidate build (OPENWIKI_NODE_MODULES)
# The paper-era .runtime also held a 3.3 GB pnpm-store-base, a 500 MB probe directory and a patched candidate
# smoke checkout; nothing in the tree reads them (O2 §14), so they are not rebuilt.
# Inputs are pinned: node by sha256, pnpm tarballs by the sha512 of package.json "packageManager" / the npm
# integrity, node_modules by the a0 pnpm-lock.yaml (every package carries an integrity). better-sqlite3 is
# compiled from source against Node 22.12.0 exactly as agentloop/evaluator/prepare_node22_runtime.sh does
# (needs python3, make, g++ on the host); the native module is checked by the same probe, not by bytes.
# Honours NPM_REGISTRY (default https://registry.npmjs.org) and NODE_DIST_URL (default https://nodejs.org/dist).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${AGENTSWE_HOME:-$PWD/.agentswe}
E=${1:-$home/envs/openwiki-runtime-v1}
registry=${NPM_REGISTRY:-https://registry.npmjs.org}; registry=${registry%/}
nodedist=${NODE_DIST_URL:-https://nodejs.org/dist}; nodedist=${nodedist%/}
NODE_VERSION=22.12.0
NODE_SHA256=22982235e1b71fa8850f82edd09cdae7e3f32df1764a9ec298c72d25ef2c164f
PNPM10_SHA512_HEX=a90faf6feeab71ad6c6e57f94e0fe1a12f5dcc22cd754db40ae9593eb6a3e0b6b12e3540218bb37ae083404b1f2ce6db2a4121e979829b4aff94b99f49da1cf8
PNPM10_LINK=cd14bace796cdc09db0cd8198931d9a2b09fe9dd4972e5c7afeceeab86491cd2
PNPM11_INTEGRITY=sha512-gTULB+U8lTigLx8jA7QpD6LXvgTlbiqXDEzEtBfcdh3hlu2r1J1Vx9yVgNuBAHxEFD5OPX5GKzAA0jwlUSLQZQ==

fetch() {  # fetch <url> <dest>: resumable, into a temporary name
  local url=$1 dest=$2
  [ -s "$dest" ] && return 0
  curl -fL --retry 5 --retry-delay 5 -C - -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
}
sha512_b64() { openssl dgst -sha512 -binary "$1" | base64 -w0; }

# Built in place (pnpm cmd-shims and the agentloop-bin wrapper embed the final path); the ready file is written
# last, so an interrupted build is redone from scratch by the next setup.
rm -rf "$E"; mkdir -p "$E"
B=$E
mkdir -p "$B/downloads" "$B/npm-cache" "$B/agentloop-bin"
dl=${AGENTSWE_HOME:-$home}/cache/downloads; mkdir -p "$dl"

# 1. Node 22.12.0
archive=node-v$NODE_VERSION-linux-x64.tar.xz
fetch "$nodedist/v$NODE_VERSION/$archive" "$dl/$archive"
echo "$NODE_SHA256  $dl/$archive" | sha256sum -c -
cp "$dl/$archive" "$B/downloads/$archive"
mkdir -p "$B/node-v$NODE_VERSION-linux-x64"
tar -xJf "$dl/$archive" --strip-components=1 -C "$B/node-v$NODE_VERSION-linux-x64"
NODE=$B/node-v$NODE_VERSION-linux-x64/bin/node
NPM_CLI=$B/node-v$NODE_VERSION-linux-x64/lib/node_modules/npm/bin/npm-cli.js
[ "$("$NODE" --version)" = "v$NODE_VERSION" ]
[ "$("$NODE" -p process.versions.modules)" = 127 ]

# 2. pnpm 10.33.2 (the a0 packageManager pin) and pnpm 11.15.1
fetch "$registry/pnpm/-/pnpm-10.33.2.tgz" "$dl/pnpm-10.33.2.tgz"
[ "$(openssl dgst -sha512 -r "$dl/pnpm-10.33.2.tgz" | cut -d' ' -f1)" = "$PNPM10_SHA512_HEX" ] \
  || { echo "pnpm-10.33.2.tgz does not match the packageManager sha512" >&2; exit 1; }
fetch "$registry/pnpm/-/pnpm-11.15.1.tgz" "$dl/pnpm-11.15.1.tgz"
[ "sha512-$(sha512_b64 "$dl/pnpm-11.15.1.tgz")" = "$PNPM11_INTEGRITY" ] \
  || { echo "pnpm-11.15.1.tgz does not match its npm integrity" >&2; exit 1; }
p10=$B/pnpm-store/v11/links/@/pnpm/10.33.2/$PNPM10_LINK/node_modules/pnpm
mkdir -p "$p10" "$B/node_modules/pnpm" "$B/node_modules/.bin"
tar -xzf "$dl/pnpm-10.33.2.tgz" --strip-components=1 --no-same-owner -C "$p10"
tar -xzf "$dl/pnpm-11.15.1.tgz" --strip-components=1 --no-same-owner -C "$B/node_modules/pnpm"
PNPM10=$p10/bin/pnpm.cjs
[ "$("$NODE" "$PNPM10" --version)" = 10.33.2 ]
# the cmd-shims pnpm wrote beside the package in its global virtual store (not read by the tree; kept for layout parity)
mkdir -p "$p10/../../bin"
for name in pnpm pnpx; do
  target=$p10/bin/$name.cjs
  cat > "$p10/../../bin/$name" <<'EOF'
#!/bin/sh
basedir=$(dirname "$(echo "$0" | sed -e 's,\\,/,g')")
basedir_win="$basedir"
exe=""
msys=""

case `uname -a` in
  *CYGWIN*|*MINGW*|*MSYS*)
    if command -v cygpath > /dev/null 2>&1; then
      basedir_win=`cygpath -w "$basedir"`
    fi
    exe=".exe"
    msys="true"
  ;;
  *WSL2*)
    if command -v wslpath > /dev/null 2>&1; then
      basedir_win="$(wslpath -w "$basedir" 2> /dev/null)"
      if [ $? -ne 0 ] || [ -z "$basedir_win" ]; then
        basedir_win="$basedir"
      else
        exe=".exe"
      fi
    fi
  ;;
esac

if [ -n "$exe" ] && [ -x "$basedir/node.exe" ]; then
  exec "$basedir/node.exe"  "$basedir_win/../node_modules/pnpm/bin/@NAME@.cjs" "$@"
elif [ -x "$basedir/node" ]; then
  exec "$basedir/node"  "$basedir/../node_modules/pnpm/bin/@NAME@.cjs" "$@"
elif command -v node >/dev/null 2>&1; then
  exec node  "$basedir/../node_modules/pnpm/bin/@NAME@.cjs" "$@"
elif [ -n "$exe" ] && command -v node.exe >/dev/null 2>&1; then
  exec node.exe  "$basedir_win/../node_modules/pnpm/bin/@NAME@.cjs" "$@"
else
  exec node  "$basedir/../node_modules/pnpm/bin/@NAME@.cjs" "$@"
fi
EOF
  sed -i "s/@NAME@/$name/g" "$p10/../../bin/$name"
  printf '# cmd-shim-target=%s\n' "$(cd "$p10/bin" && pwd)/$name.cjs" >> "$p10/../../bin/$name"
  chmod 0755 "$p10/../../bin/$name"
done

# 3. agentloop-bin/pnpm (absolute paths to the final env dir, as in the paper-era wrapper)
cat > "$B/agentloop-bin/pnpm" <<EOF
#!/bin/sh
ROOT=$E
exec "\$ROOT/node-v$NODE_VERSION-linux-x64/bin/node" \\
  "\$ROOT/pnpm-store/v11/links/@/pnpm/10.33.2/$PNPM10_LINK/node_modules/pnpm/bin/pnpm.cjs" \\
  "\$@"
EOF
chmod 0755 "$B/agentloop-bin/pnpm"

# 4. the offline dependency tree: a0's package.json + pnpm-lock.yaml + pnpm-workspace.yaml, frozen install
R=$B/candidate-smoke/repository
mkdir -p "$R"
(cd "$here/candidate-smoke" && sha256sum -c --quiet "$here/candidate-smoke.sha256")
cp "$here/candidate-smoke/package.json" "$here/candidate-smoke/pnpm-lock.yaml" "$here/candidate-smoke/pnpm-workspace.yaml" "$R/"
store=$B/.install-store
(cd "$R" && PATH="$(dirname "$NODE"):/usr/bin:/bin" npm_config_registry="$registry/" \
  npm_config_fetch_retries=6 npm_config_fetch_retry_mintimeout=10000 npm_config_fetch_retry_maxtimeout=120000 \
  npm_config_fetch_timeout=300000 \
  "$NODE" "$PNPM10" install --frozen-lockfile --ignore-scripts --store-dir "$store" --package-import-method copy \
  --config.confirmModulesPurge=false --network-concurrency 8 --reporter append-only)
# --ignore-scripts: the paper-era tree has better-sqlite3 and esbuild in .modules.yaml pendingBuilds (no install
# scripts ran); better-sqlite3 is compiled below, esbuild uses its @esbuild/linux-x64 binary untouched.
rm -rf "$store"
[ -d "$R/node_modules/.pnpm" ]

# 5. better-sqlite3 from source against Node 22.12.0 (prepare_node22_runtime.sh)
PYTHONDONTWRITEBYTECODE=1 PATH="$(dirname "$NODE"):/usr/bin:/bin" npm_config_build_from_source=true \
npm_config_nodedir="$B/node-v$NODE_VERSION-linux-x64" npm_config_cache="$B/npm-cache" \
  "$NODE" "$NPM_CLI" rebuild better-sqlite3 --foreground-scripts --prefix "$R"
pkg=$(find "$R/node_modules/.pnpm" -maxdepth 1 -type d -name 'better-sqlite3@*' -print -quit)/node_modules/better-sqlite3
# The probe of prepare_node22_runtime.sh, with the SQL literal single-quoted: better-sqlite3 12 is compiled with
# SQLITE_DQS=0 and rejects the double-quoted literal that script uses (the paper-era module rejects it too).
PKG=$pkg PATH="$(dirname "$NODE"):/usr/bin:/bin" "$NODE" <<'JS'
const Database = require(require("node:path").resolve(process.env.PKG));
const db = new Database(":memory:");
db.exec("CREATE TABLE runtime_probe (value TEXT NOT NULL); INSERT INTO runtime_probe VALUES ('node22-native-ok')");
const row = db.prepare("SELECT value FROM runtime_probe").get();
db.close();
if (process.version !== "v22.12.0" || row.value !== "node22-native-ok" || db.open) process.exit(1);
process.stdout.write(JSON.stringify({node: process.version, modules: process.versions.modules, value: row.value, closed: !db.open}) + "\n");
JS

# 6. done
date -u +%Y-%m-%dT%H:%M:%SZ > "$E/.agentswe-env-complete"
echo "built $E"
