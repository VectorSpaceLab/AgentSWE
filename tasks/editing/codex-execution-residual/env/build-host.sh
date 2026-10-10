#!/bin/bash
# Host-side build of codex-project-memory-edit-v1: the evaluator's Rust toolchain for building the Candidate codex CLI.
# usage: build-host.sh <env dir>   (default $AGENTSWE_HOME/envs/codex-project-memory-edit-v1)
#
# The evaluator (harbor/build_preflight.py) uses <env>/bin/cargo and <env>/bin/rustc, OPENSSL_DIR=<env>,
# PKG_CONFIG_PATH=<env>/lib/pkgconfig, CARGO_HOME=<env>/cargo-home (offline: CARGO_NET_OFFLINE=true) and an
# empty RUSTUP_HOME, and runs cargo from host paths inside bubblewrap, so the prefix is built on the host.
#   1. conda prefix: the 39 conda-forge packages of the paper environment (conda-explicit.txt; rust 1.95.0,
#      openssl 3.6.3, python 3.11.15, pkg-config, just, gcc_impl ...), fetched from AGENTSWE_CONDA_CHANNEL and
#      checked against their sha256, installed with micromamba directly at <env dir>.
#   2. cargo-home: `cargo fetch --locked` of the fetched a0's codex-rs/Cargo.lock with that cargo (crates are
#      checked against the lock's checksums; git dependencies are pinned by revision). Rebuilt this way the crate
#      cache (1,155 .crate files) and the 6 git checkouts are byte-identical to the paper environment's.
#      If AGENTSWE_ENV_ARCHIVE_DIR holds the pinned archive of the paper environment's cargo-home, it is used instead
#      (offline, same content; sha256 pinned below).
#   3. the rusty_v8 v150.4.0 prebuilt static library that the v8 crate's build script would otherwise download,
#      placed where it looks for it (cargo-home/.rusty_v8/<url-encoded name>), sha256-pinned. The upstream release
#      asset is pinned; the paper environment held a locally recompressed copy of the same library (decompressed
#      bytes identical, sha256 453df876...).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${AGENTSWE_HOME:-$PWD/.agentswe}
name=codex-project-memory-edit-v1
envdir=${1:-$home/envs/$name}
channel=${AGENTSWE_CONDA_CHANNEL:-https://conda.anaconda.org/conda-forge}
channel=${channel%/}
mm=${MICROMAMBA:-$home/tools/micromamba}
a0=$home/editing/tasks/codex-execution-residual/tree/input/repository
v8_url=https://github.com/denoland/rusty_v8/releases/download/v150.4.0/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz
v8_name=https___github_com_denoland_rusty_v8_releases_download_v150_4_0_librusty_v8_release_x86_64_unknown_linux_gnu_a_gz
v8_sha256=5869fdb7071b1f21f200a97ce97d20125877e0f31cd802ed99de2c53f488b06c
v8_lib_sha256=453df8763f06442fc27d034d2fa3099fa606be67fa99da04324e1733c02f18c1
cargo_home_archive_sha256=629db2d6482a2fcd20e66026f6ee613cc62b3e2695a2805e6fb7ec45b4a66140
lock_sha256=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cargo_lock_sha256"])' "$here/env.json")

test -x "$mm" || { echo "micromamba not found at $mm" >&2; exit 1; }
[ -f "$a0/codex-rs/Cargo.lock" ] || { echo "a0 not found under $a0 (setup fetches a0 before building environments)" >&2; exit 1; }
[ "$(sha256sum "$a0/codex-rs/Cargo.lock" | cut -d' ' -f1)" = "$lock_sha256" ] || { echo "a0 Cargo.lock is not the pinned one" >&2; exit 1; }
if [ -e "$envdir" ]; then
  echo "$envdir exists but is incomplete; remove it and rerun setup" >&2
  exit 1
fi
if [ -n "${AGENTSWE_BUILD_PROXY:-}" ]; then
  export https_proxy=$AGENTSWE_BUILD_PROXY http_proxy=$AGENTSWE_BUILD_PROXY HTTPS_PROXY=$AGENTSWE_BUILD_PROXY HTTP_PROXY=$AGENTSWE_BUILD_PROXY
  export no_proxy=${AGENTSWE_BUILD_NO_PROXY:-localhost,127.0.0.1} NO_PROXY=${AGENTSWE_BUILD_NO_PROXY:-localhost,127.0.0.1}
fi

# 1. conda packages, sha256-checked
pkgs=$home/cache/conda-pkgs
mkdir -p "$pkgs"
explicit=$(mktemp "$home/cache/$name.explicit.XXXXXX")
trap 'rm -f "$explicit"' EXIT
echo "@EXPLICIT" > "$explicit"
while read -r sub sha md5; do
  case "$sub" in ''|'#'*) continue ;; esac
  f=$pkgs/$(basename "$sub")
  if ! { [ -f "$f" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sha" ]; }; then
    curl -fL --retry 5 --retry-all-errors --connect-timeout 30 -sS -o "$f.part" "$channel/$sub"
    mv "$f.part" "$f"
  fi
  got=$(sha256sum "$f" | cut -d' ' -f1)
  [ "$got" = "$sha" ] || { echo "sha256 mismatch for $sub: $got != $sha" >&2; exit 1; }
  echo "file://$f#$md5" >> "$explicit"
done < "$here/conda-explicit.txt"
"$mm" create -y -q -p "$envdir" -r "$home/tools/mamba-root" --file "$explicit"

mkdir -p "$envdir/rustup-home"
archive=${AGENTSWE_ENV_ARCHIVE_DIR:+$AGENTSWE_ENV_ARCHIVE_DIR/$name.cargo-home.tar.zst}
if [ -n "$archive" ] && [ -f "$archive" ]; then
  # 2a. fast path: the pinned archive of the paper environment's cargo-home (same crates and git checkouts as the
  #     recipe below; its .rusty_v8 file is the paper's local recompression of the same library)
  got=$(sha256sum "$archive" | cut -d' ' -f1)
  [ "$got" = "$cargo_home_archive_sha256" ] || { echo "sha256 mismatch for $archive: $got" >&2; exit 1; }
  zstd -dc "$archive" | tar -C "$envdir" -xf -
else
  # 2b. recipe: offline Cargo home for the a0 workspace. Crates are verified against the lock's checksums and kept in
  #     CARGO_HOME between attempts, so a slow or flaky link to crates.io only costs retries.
  mkdir -p "$envdir/cargo-home"
  for attempt in 1 2 3 4 5 6 7 8; do
    if env -i PATH="$envdir/bin:/usr/bin:/bin" HOME="$home/tmp" CARGO_HOME="$envdir/cargo-home" RUSTUP_HOME="$envdir/rustup-home" \
        CARGO_NET_GIT_FETCH_WITH_CLI=true CARGO_HTTP_MULTIPLEXING=false CARGO_NET_RETRY=10 CARGO_HTTP_TIMEOUT=120 \
        ${https_proxy:+https_proxy=$https_proxy http_proxy=$http_proxy no_proxy=$no_proxy} \
        "$envdir/bin/cargo" fetch --locked --manifest-path "$a0/codex-rs/Cargo.toml"; then
      break
    fi
    [ "$attempt" = 8 ] && { echo "cargo fetch did not complete after 8 attempts" >&2; exit 1; }
    echo "cargo fetch attempt $attempt failed; resuming" >&2
  done
  # 3. rusty_v8 prebuilt that the v8 crate's build script would otherwise download
  mkdir -p "$envdir/cargo-home/.rusty_v8"
  curl -fL --retry 10 --retry-all-errors --connect-timeout 30 -sS -o "$envdir/cargo-home/.rusty_v8/$v8_name.part" "$v8_url"
  [ "$(sha256sum "$envdir/cargo-home/.rusty_v8/$v8_name.part" | cut -d' ' -f1)" = "$v8_sha256" ] || { echo "rusty_v8 sha256 mismatch" >&2; exit 1; }
  mv "$envdir/cargo-home/.rusty_v8/$v8_name.part" "$envdir/cargo-home/.rusty_v8/$v8_name"
fi
v8_file=$envdir/cargo-home/.rusty_v8/$v8_name
[ "$(gzip -dc "$v8_file" | sha256sum | cut -d' ' -f1)" = "$v8_lib_sha256" ] || { echo "rusty_v8 library sha256 mismatch" >&2; exit 1; }

# 4. the evaluator's own probes
env -i PATH="$envdir/bin:/usr/bin:/bin" CARGO_HOME="$envdir/cargo-home" CARGO_NET_OFFLINE=true \
  "$envdir/bin/cargo" fetch --locked --offline --manifest-path "$a0/codex-rs/Cargo.toml"
"$envdir/bin/cargo" --version
"$envdir/bin/rustc" --version
date -u +%FT%TZ > "$envdir/.agentswe-built"   # setup's ready_file marker: written last
echo "built $envdir"
