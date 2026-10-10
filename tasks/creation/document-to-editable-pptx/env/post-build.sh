#!/bin/bash
# Playwright 1.61.0 browsers of the paper-era document-to-editable-pptx environment
# (chromium-1228, ffmpeg-1011), installed from the Playwright CDN (mirror hook: PLAYWRIGHT_DOWNLOAD_HOST).
set -euo pipefail
: "${PREFIX:?}"
PLAYWRIGHT_BROWSERS_PATH="$PREFIX/playwright-browsers" "$PREFIX/bin/python" -m playwright install --no-shell chromium
for b in chromium-1228 ffmpeg-1011; do test -d "$PREFIX/playwright-browsers/$b"; done
# aspose-legacy/: native libraries of the legacy Aspose.Slides renderer, unpacked by hand in the paper
# environment (no package metadata except the last conda package's info/).  Same files and layout from the
# env.json "downloads" (sha256-pinned, handed over in /spec/downloads): conda-forge openssl 1.1.1w, libtiff 4.3.0
# and jpeg 9e, then Ubuntu jammy libgdiplus 6.0.4+dfsg-2 and libexif12 0.6.24-1build1.
A="$PREFIX/aspose-legacy"
mkdir -p "$A"
for pkg in openssl-1.1.1w-hd590300_0.conda libtiff-4.3.0-h0fcbabc_4.tar.bz2 jpeg-9e-h0b41bf4_3.conda; do
  case "$pkg" in
    *.conda) t=$(mktemp -d); (cd "$t" && unzip -q "/spec/downloads/$pkg" && for z in pkg-*.tar.zst info-*.tar.zst; do tar --zstd -xf "$z" -C "$A"; done); rm -rf "$t" ;;
    *.tar.bz2) tar -xjf "/spec/downloads/$pkg" -C "$A" ;;
  esac
done
for deb in libgdiplus_6.0.4+dfsg-2_amd64.deb libexif12_0.6.24-1build1_amd64.deb; do dpkg-deb -x "/spec/downloads/$deb" "$A"; done
test -f "$A/lib/libssl.so.1.1" && test -f "$A/lib/libtiff.so.5.7.0" && test -f "$A/usr/lib/libgdiplus.so.0.0.0"
# NOT YET REPRODUCED: 21 pillow files that pip left outside its RECORD.  See env.json "open_items".
