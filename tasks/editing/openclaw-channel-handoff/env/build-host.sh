#!/bin/bash
# Host-side install of openclaw-channel-handoff-ledger-edit-v1 from its pinned archive (archive-first release).
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/openclaw-channel-handoff-ledger-edit-v1)
# The archive is the runtime-used subset of the paper environment (see env.json "archive"). It is looked up in
# $AGENTSWE_ENV_ARCHIVE_DIR (or downloaded from $AGENTSWE_ENV_ARCHIVE_URL/<name> when that is set), checked
# against the pinned sha256, unpacked, and every unpacked path is checked against the pinned content manifest.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${AGENTSWE_HOME:?AGENTSWE_HOME must be set}
prefix=${1:-$home/envs/openclaw-channel-handoff-ledger-edit-v1}
case "$prefix" in "$home"/envs/*) ;; *) echo "refusing to install outside $home/envs: $prefix" >&2; exit 1;; esac
read -r archive archive_sha manifest manifest_sha < <(python3 -c '
import json,sys; a=json.load(open(sys.argv[1]))["archive"]
print(a["name"], a["sha256"], a["manifest"], a["manifest_sha256"])' "$here/env.json")
marker=$prefix/.agentswe-env-ready
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$archive_sha" ]; then echo "up to date: $prefix"; exit 0; fi
fetch() {  # $1 = file name; prints the local path
  if [ -n "${AGENTSWE_ENV_ARCHIVE_DIR:-}" ] && [ -f "$AGENTSWE_ENV_ARCHIVE_DIR/$1" ]; then
    echo "$AGENTSWE_ENV_ARCHIVE_DIR/$1"; return
  fi
  if [ -n "${AGENTSWE_ENV_ARCHIVE_URL:-}" ]; then
    mkdir -p "$home/cache/downloads"
    curl -fL -C - --retry 5 -sS -o "$home/cache/downloads/$1" "$AGENTSWE_ENV_ARCHIVE_URL/$1" >&2
    echo "$home/cache/downloads/$1"; return
  fi
  echo "environment archive $1 not found: set AGENTSWE_ENV_ARCHIVE_DIR (a directory holding it) or AGENTSWE_ENV_ARCHIVE_URL" >&2
  return 1
}
a=$(fetch "$archive"); m=$(fetch "$manifest")
test "$(sha256sum "$a" | cut -d' ' -f1)" = "$archive_sha" || { echo "sha256 mismatch: $a" >&2; exit 1; }
test "$(sha256sum "$m" | cut -d' ' -f1)" = "$manifest_sha" || { echo "sha256 mismatch: $m" >&2; exit 1; }
partial=$prefix.partial
rm -rf "$partial" "$prefix"
mkdir -p "$partial"
zstd -dc "$a" | tar -C "$partial" --numeric-owner -xf -
python3 - "$partial" "$m" <<'PY'
import hashlib, json, os, stat, sys
root, manifest = sys.argv[1], sys.argv[2]
want = {}
for line in open(manifest):
    row = json.loads(line); want[row["path"]] = row
seen = set(); bad = []
for part in sorted({p.split("/", 1)[0] for p in want}):
    for d, dirs, files in os.walk(os.path.join(root, part)):
        for name in [d] + [os.path.join(d, x) for x in dirs + files]:
            rel = os.path.relpath(name, root)
            if rel in seen: continue
            seen.add(rel)
for rel in sorted(set(want) | seen):
    row = want.get(rel)
    if row is None: bad.append("unexpected " + rel); continue
    p = os.path.join(root, rel)
    if not os.path.lexists(p): bad.append("missing " + rel); continue
    st = os.lstat(p)
    if oct(stat.S_IMODE(st.st_mode)) != row["mode"]: bad.append("mode " + rel)
    if "link" in row:
        if not stat.S_ISLNK(st.st_mode) or os.readlink(p) != row["link"]: bad.append("link " + rel)
    elif "sha256" in row:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
        if not stat.S_ISREG(st.st_mode) or st.st_size != row["size"] or h.hexdigest() != row["sha256"]: bad.append("content " + rel)
    if len(bad) > 20: break
if bad:
    sys.exit("environment does not match its manifest:\n  " + "\n  ".join(bad[:20]))
print("manifest verified: %d paths" % len(want))
PY
mv "$partial" "$prefix"
echo "$archive_sha" > "$marker"
echo "installed $prefix"
