#!/usr/bin/env bash
# Materialise this task's a0 (input/repository) from the pinned upstream commit.
#   fetch.sh <dest-dir> [--lang en-upstream] [--verify]
# The public release materializes only the pinned English upstream tree.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
dest=${1:?usage: fetch.sh <dest> [--lang en-upstream] [--verify]}; shift
lang="en-upstream"; verify=0
while [ $# -gt 0 ]; do case "$1" in --lang) [ $# -ge 2 ] && [ "$2" = en-upstream ] || { echo "only en-upstream is available in the public release" >&2; exit 2; }; shift 2;; --verify) verify=1; shift;; *) echo "unknown arg $1" >&2; exit 2;; esac; done
repo=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["repo"])' "$here/a0.json")
commit=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["commit"])' "$here/a0.json")
tree=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["git_tree"])' "$here/a0.json")
[ -e "$dest" ] && { echo "refusing to overwrite $dest" >&2; exit 1; }
# Source: git (tree hash checked) or, when git transfer is unavailable or AGENTSWE_A0_SOURCE=codeload,
# the commit tarball from GitHub codeload (then verified file by file against en-upstream.sha256 below).
tarball=0
if [ "${AGENTSWE_A0_SOURCE:-git}" = git ] && git init -q "$dest" \
   && timeout "${AGENTSWE_A0_GIT_TIMEOUT:-900}" git -C "$dest" fetch -q --depth 1 "$repo" "$commit" \
   && git -C "$dest" -c advice.detachedHead=false checkout -q FETCH_HEAD; then
  got=$(git -C "$dest" rev-parse 'HEAD^{tree}')
  [ "$got" = "$tree" ] || { echo "upstream tree mismatch: $got != $tree" >&2; exit 1; }
  rm -rf "$dest/.git"
else
  echo "git transfer unavailable; using the codeload tarball of $commit" >&2
  rm -rf "$dest"; mkdir -p "$dest"
  slug=${repo#https://github.com/}; slug=${slug%.git}
  curl -fsSL --retry 3 "${AGENTSWE_GITHUB_CODELOAD:-https://codeload.github.com}/$slug/tar.gz/$commit" \
    | tar -xz -C "$dest" --strip-components=1 --no-same-owner
  tarball=1
fi
if [ -f "$here/subset.txt" ]; then   # a0 is a subset of the upstream commit
  python3 - "$dest" "$here/subset.txt" <<'PY'
import os, sys
root, keep = sys.argv[1], set(open(sys.argv[2]).read().split('\n')) - {''}
for d, dn, fn in os.walk(root, topdown=False):
    for f in fn:
        rel = os.path.relpath(os.path.join(d, f), root)
        if rel not in keep: os.remove(os.path.join(d, f))
    if d != root and not os.listdir(d): os.rmdir(d)
PY
fi
if [ "$tarball" = 1 ]; then   # no git tree hash on this path: the pinned upstream content must match exactly
  (cd "$dest" && sha256sum --quiet -c "$here/en-upstream.sha256") || { echo "upstream content mismatch" >&2; exit 1; }
fi
manifest="$here/en-upstream.sha256"
if [ "$verify" = 1 ]; then (cd "$dest" && sha256sum --quiet -c "$manifest"); echo "a0 verified against $(basename "$manifest")"; fi
