#!/bin/bash
# formal-theorem-proving-agent-v2 = conda Python 3.11.15 (conda-explicit.txt) plus the
# official Lean 4.19.0 linux release unpacked into the prefix root (bin/lean, lake,
# leanc, clang, ld.lld, lib/lean, include/lean, src/lean, share/lean, LICENSE, LICENSES).
# Mirror hook: LEAN_RELEASE_URL.  LEAN_TARBALL_SHA256 must be filled in once the asset
# is fetched from a host with GitHub release access (the OI build host could not reach release assets).
set -euo pipefail
: "${PREFIX:?}"
url=${LEAN_RELEASE_URL:-https://github.com/leanprover/lean4/releases/download/v4.19.0/lean-4.19.0-linux.tar.zst}
curl -fsSL --retry 5 -o /tmp/lean.tar.zst "$url"
if [ -n "${LEAN_TARBALL_SHA256:-}" ]; then echo "${LEAN_TARBALL_SHA256}  /tmp/lean.tar.zst" | sha256sum -c -; fi
mkdir -p /tmp/lean && tar --zstd -xf /tmp/lean.tar.zst -C /tmp/lean --strip-components=1
# The release bundles its own libgcc_s.so.1 and libatomic.so.1.2.0; the paper environment kept the conda copies
# (902,640 / 172,632 bytes), so files already in the prefix are not overwritten.
cp -a --update=none /tmp/lean/. "$PREFIX/"
rm -rf /tmp/lean /tmp/lean.tar.zst
test "$("$PREFIX/bin/lean" --version | grep -o '4\.19\.0' | head -1)" = 4.19.0
