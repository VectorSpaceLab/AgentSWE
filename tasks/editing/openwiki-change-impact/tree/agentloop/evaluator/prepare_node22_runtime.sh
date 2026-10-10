#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="22.12.0"
ARCHIVE="node-v${VERSION}-linux-x64.tar.xz"
EXPECTED_SHA256="22982235e1b71fa8850f82edd09cdae7e3f32df1764a9ec298c72d25ef2c164f"
DOWNLOADS="$ROOT/.runtime/downloads"
NODE_ROOT="$ROOT/.runtime/node-v${VERSION}-linux-x64"
REPOSITORY="${1:-$ROOT/.runtime/candidate-smoke/repository}"

mkdir -p "$DOWNLOADS" "$ROOT/.runtime/npm-cache"
if [[ ! -f "$DOWNLOADS/$ARCHIVE" ]]; then
  curl -fsSLo "$DOWNLOADS/$ARCHIVE" \
    "https://nodejs.org/dist/v${VERSION}/${ARCHIVE}"
fi
printf '%s  %s\n' "$EXPECTED_SHA256" "$DOWNLOADS/$ARCHIVE" | sha256sum -c -

if [[ ! -x "$NODE_ROOT/bin/node" ]]; then
  temporary="$NODE_ROOT.tmp.$$"
  rm -rf "$temporary"
  mkdir -p "$temporary"
  tar -xJf "$DOWNLOADS/$ARCHIVE" --strip-components=1 -C "$temporary"
  rm -rf "$NODE_ROOT"
  mv "$temporary" "$NODE_ROOT"
fi

NODE="$NODE_ROOT/bin/node"
NPM_CLI="$NODE_ROOT/lib/node_modules/npm/bin/npm-cli.js"
[[ "$($NODE --version)" == "v${VERSION}" ]]
[[ "$($NODE -p 'process.versions.modules')" == "127" ]]
[[ -d "$REPOSITORY/node_modules/.pnpm" ]]

PATH="$NODE_ROOT/bin:/usr/bin:/bin" \
npm_config_build_from_source=true \
npm_config_nodedir="$NODE_ROOT" \
npm_config_cache="$ROOT/.runtime/npm-cache" \
  "$NODE" "$NPM_CLI" rebuild better-sqlite3 --foreground-scripts

PACKAGE="$(find "$REPOSITORY/node_modules/.pnpm" -maxdepth 1 -type d \
  -name 'better-sqlite3@*' -print -quit)/node_modules/better-sqlite3"
[[ -d "$PACKAGE" ]]

PATH="$NODE_ROOT/bin:/usr/bin:/bin" "$NODE" -e '
const path = require("node:path");
const Database = require(path.resolve(process.argv[1]));
const db = new Database(":memory:");
db.exec("CREATE TABLE runtime_probe (value TEXT NOT NULL); INSERT INTO runtime_probe VALUES (\"node22-native-ok\")");
const row = db.prepare("SELECT value FROM runtime_probe").get();
db.close();
if (process.version !== "v22.12.0" || row.value !== "node22-native-ok" || db.open) process.exit(1);
process.stdout.write(JSON.stringify({node: process.version, modules: process.versions.modules, value: row.value, closed: !db.open}) + "\n");
' "$PACKAGE"

sha256sum "$DOWNLOADS/$ARCHIVE" \
  "$PACKAGE/build/Release/better_sqlite3.node" \
  "$NODE"
