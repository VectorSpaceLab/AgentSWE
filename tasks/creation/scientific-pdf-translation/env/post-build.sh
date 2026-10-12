#!/bin/bash
# Non-conda, non-pip content of scientific-pdf-translation-agent-v2, reproduced
# from upstream sources (run by agentswe-build-env with PREFIX set):
#   1. Playwright 1.54.0 browsers: chromium-1181, chromium_headless_shell-1181, ffmpeg-1011
#   2. pdfjs-dist 4.10.38 (npm tarball kept, unpacked to pdfjs-dist/ and share/pdfjs-dist-4.10.38/)
#   3. WenQuanYi Zen Hei (Ubuntu noble fonts-wqy-zenhei 0.9.45-8) at share/fonts/wqy-zenhei.ttc
#   4. fontconfig cache
# Mirror hooks: PLAYWRIGHT_DOWNLOAD_HOST, NPM_REGISTRY, UBUNTU_ARCHIVE_URL.
set -euo pipefail
: "${PREFIX:?PREFIX must point at the environment}"

PLAYWRIGHT_BROWSERS_PATH="$PREFIX/playwright-browsers" "$PREFIX/bin/python" -m playwright install chromium
test -d "$PREFIX/playwright-browsers/chromium-1181" -a -d "$PREFIX/playwright-browsers/chromium_headless_shell-1181" -a -d "$PREFIX/playwright-browsers/ffmpeg-1011"

PDFJS=pdfjs-dist-4.10.38.tgz
mkdir -p "$PREFIX/npm-packages" "$PREFIX/pdfjs-dist" "$PREFIX/share/pdfjs-dist-4.10.38"
npm pack pdfjs-dist@4.10.38 --ignore-scripts --pack-destination "$PREFIX/npm-packages" \
    --registry "${NPM_REGISTRY:-https://registry.npmjs.org}" --cache /tmp/agentswe-npm-cache --loglevel warn
echo "1011b38553532d7078c59f26b15a471f8dae00f101b60e2add9b8511737a1ce0  $PREFIX/npm-packages/$PDFJS" | sha256sum -c -
tar -xzf "$PREFIX/npm-packages/$PDFJS" -C "$PREFIX/pdfjs-dist" --strip-components=1 --no-same-owner
tar -xzf "$PREFIX/npm-packages/$PDFJS" -C "$PREFIX/share/pdfjs-dist-4.10.38" --strip-components=1 --no-same-owner
rm -rf /tmp/agentswe-npm-cache

FONT_DEB=fonts-wqy-zenhei_0.9.45-8_all.deb
curl -fsSL --retry 5 -o "/tmp/$FONT_DEB" "${UBUNTU_ARCHIVE_URL:-http://archive.ubuntu.com/ubuntu}/pool/universe/f/fonts-wqy-zenhei/$FONT_DEB"
echo "496969d65dc5664a3291e678d5cbef6815dc308ee5bf37b656c00af2cb490410  /tmp/$FONT_DEB" | sha256sum -c -
dpkg-deb -x "/tmp/$FONT_DEB" /tmp/wqy
mkdir -p "$PREFIX/share/fonts"
install -m 0644 /tmp/wqy/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc "$PREFIX/share/fonts/wqy-zenhei.ttc"
echo "79c18ebe7b811951e8311bad7103ebeae8c337ed9988ea69e8a78a66cfe029b9  $PREFIX/share/fonts/wqy-zenhei.ttc" | sha256sum -c -
rm -rf /tmp/wqy "/tmp/$FONT_DEB"

"$PREFIX/bin/fc-cache" -f >/dev/null 2>&1 || true
