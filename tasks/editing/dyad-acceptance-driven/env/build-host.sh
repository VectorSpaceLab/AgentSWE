#!/bin/bash
# Host-side build of the three Dyad environments the trees read from host paths:
#   @@AGENTSWE_ENVS@@/dyad-task-env-cycle-006  dyad 1.9.0 at the a0 pin plus its installed dependencies (node_modules,
#                                             testing/fake-llm-server/node_modules, two worker tsbuildinfo files)
#   @@AGENTSWE_ENVS@@/runtime-deps-0909        Playwright 1.58.2 browsers: Chrome for Testing 145.0.7632.6 (chromium
#                                             and chrome-headless-shell, revision 1208) and ffmpeg 1011
#   @@AGENTSWE_ENVS@@/runtime-deps-0910        node 24.6.0 (bin/node only)
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/dyad-task-env-cycle-006; the two runtime-deps
#                                  directories are created beside it)
#
# Sources, each pinned by sha256 and checked again by content against the paper hosts' directories:
#   - dyad source: the task's own a0 fetch (en-upstream at the a0 commit, verified file by file);
#   - dependencies: the pinned archive dyad-cycle-006-deps.tar.zst in AGENTSWE_ENV_ARCHIVE_DIR (archive first: the
#     paper install ran npm ci with postinstall builds and an Electron download; a recipe is planned);
#   - node: the official node-v24.6.0-linux-x64.tar.xz (NODE_DIST_URL, default https://nodejs.org/dist);
#   - browsers: the official Chrome for Testing zips (AGENTSWE_CFT_BASE_URL to use a mirror) and Playwright's ffmpeg
#     zip (AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST to use a mirror), unpacked the way Playwright's installer does.
# A downloaded file already in $AGENTSWE_HOME/cache/downloads with the pinned sha256 is used as is.
set -euo pipefail
umask 022
here=$(cd "$(dirname "$0")" && pwd)
task=$(dirname "$here")
home=${AGENTSWE_HOME:-$PWD/.agentswe}
envdir=${1:-$home/envs/dyad-task-env-cycle-006}
envs=$(dirname "$envdir")
deps0909=$envs/runtime-deps-0909
deps0910=$envs/runtime-deps-0910
cache=$home/cache/downloads
mkdir -p "$cache"

# content manifests of the paper directories (env_manifest.py digests)
env_manifest_sha256=83b21fd88b43eaeac4538375f8d4a170e8ff41a04849d5d23a6b3583e74b3ae4      # 95397 entries
deps0909_manifest_sha256=26eae8d29304f26173c8f2a3ebafd5a60e19e542a609d47cb0e783a6e7ea5a73   # 620 entries
deps0910_manifest_sha256=ddf5186b2e200cc9c97742494b2584c819165512c5892006c1e1336741e22d07   # 4 entries

archive_name=dyad-cycle-006-deps.tar.zst
archive_sha256=f5f0ae6df4c5dd1c99170ad400f7fd62ebabeef4e4aa8a707609d96953a560d6
node_tar=node-v24.6.0-linux-x64.tar.xz
node_tar_sha256=fda6f6a00759eea0a27e34fcdfdd09c2b0413855edaa7f746246cf81c0186e26
node_bin_sha256=e943ee9282bef08233665cb71cc57a9f5794bbe70a4822b38e60e394c15979e2
cft=${AGENTSWE_CFT_BASE_URL:-https://storage.googleapis.com/chrome-for-testing-public}/145.0.7632.6/linux64
pwhost=${AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST:-https://cdn.playwright.dev/dbazure/download/playwright}

fetch() {  # fetch <name> <sha256> <url>: resumable download into the cache, sha-checked
  local name=$1 want=$2 url=$3 path=$cache/$1
  if [ -f "$path" ] && [ "$(sha256sum "$path" | cut -d' ' -f1)" = "$want" ]; then return; fi
  rm -f "$path"
  local i
  for i in $(seq 1 30); do
    curl -fL --http1.1 -C - --retry 5 --connect-timeout 30 -sS -o "$path.part" "$url" && break
    [ "$i" = 30 ] && { echo "download did not complete: $url" >&2; exit 1; }
  done
  mv "$path.part" "$path"
  [ "$(sha256sum "$path" | cut -d' ' -f1)" = "$want" ] || { echo "sha256 mismatch for $url" >&2; exit 1; }
}

check() {  # check <dir> <pinned manifest sha256> [--exclude ...]
  local dir=$1 want=$2; shift 2
  local got
  got=$(python3 -B "$here/env_manifest.py" "$dir" "$@" | cut -d' ' -f1)
  [ "$got" = "$want" ] || { echo "$dir does not match the paper environment (manifest $got, want $want)" >&2; exit 1; }
}

for d in "$envdir" "$deps0909" "$deps0910"; do
  if [ -e "$d" ]; then echo "$d exists but is incomplete; remove it and rerun setup" >&2; exit 1; fi
done

# runtime-deps-0910: node 24.6.0
fetch "$node_tar" "$node_tar_sha256" "${NODE_DIST_URL:-https://nodejs.org/dist}/v24.6.0/$node_tar"
mkdir -p "$deps0910/dyad/node24.6.0/bin"
tar -xJf "$cache/$node_tar" -C "$deps0910/dyad/node24.6.0/bin" --strip-components=2 node-v24.6.0-linux-x64/bin/node
[ "$(sha256sum "$deps0910/dyad/node24.6.0/bin/node" | cut -d' ' -f1)" = "$node_bin_sha256" ]
check "$deps0910" "$deps0910_manifest_sha256"

# runtime-deps-0909: Playwright's browser directory layout (as `playwright install chromium` leaves it)
fetch chrome-linux64.zip b5e3195041af345a668d110f5daf5581961fa3608626ea588c97dd0fe81c4e38 "$cft/chrome-linux64.zip"
fetch chrome-headless-shell-linux64.zip 2536e97d8f410df0394b3e7c4252e88ce9f239f04f3af4e247a26caf45baf49e \
  "$cft/chrome-headless-shell-linux64.zip"
fetch ffmpeg-1011-linux.zip ebc74fc5b94830176a3c2914ae96bd8bc7f6a91f4f33890230f84a172ee61ccc \
  "$pwhost/builds/ffmpeg/1011/ffmpeg-linux.zip"
pw=$deps0909/dyad/ms-playwright
python3 -B "$here/unzip_modes.py" "$cache/chrome-linux64.zip" "$pw/chromium-1208"
python3 -B "$here/unzip_modes.py" "$cache/chrome-headless-shell-linux64.zip" "$pw/chromium_headless_shell-1208"
python3 -B "$here/unzip_modes.py" "$cache/ffmpeg-1011-linux.zip" "$pw/ffmpeg-1011"
for d in chromium-1208 chromium_headless_shell-1208 ffmpeg-1011; do
  : > "$pw/$d/INSTALLATION_COMPLETE"; : > "$pw/$d/DEPENDENCIES_VALIDATED"
done
check "$deps0909" "$deps0909_manifest_sha256"

# dyad-task-env-cycle-006: the dyad source at the a0 pin (English upstream) plus the pinned dependency layer
archive=${AGENTSWE_ENV_ARCHIVE_DIR:+$AGENTSWE_ENV_ARCHIVE_DIR/$archive_name}
if [ -z "$archive" ] || [ ! -f "$archive" ]; then
  echo "dyad-task-env-cycle-006 needs the pinned archive $archive_name (sha256 $archive_sha256) in AGENTSWE_ENV_ARCHIVE_DIR" >&2
  exit 1
fi
[ "$(sha256sum "$archive" | cut -d' ' -f1)" = "$archive_sha256" ] || { echo "sha256 mismatch for $archive" >&2; exit 1; }
bash "$task/a0/fetch.sh" "$envdir" --lang en-upstream --verify
# the paper copy was a git checkout (0644/0755); a codeload tarball carries group-writable modes
chmod -R go-w "$envdir"
zstd -dc "$archive" | tar -C "$envdir" -xf -
check "$envdir" "$env_manifest_sha256"
date -u +%FT%TZ > "$envdir/.agentswe-built"   # setup's ready_file marker: written last
echo "built $envdir, $deps0909, $deps0910"
