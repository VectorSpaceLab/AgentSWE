#!/bin/bash
# Playwright 1.61.0 browsers of the paper-era desktop-gui-automation environment
# (chromium-1228, ffmpeg-1011), installed from the Playwright CDN (mirror hook: PLAYWRIGHT_DOWNLOAD_HOST).
set -euo pipefail
: "${PREFIX:?}"
PLAYWRIGHT_BROWSERS_PATH="$PREFIX/browsers" "$PREFIX/bin/python" -m playwright install --no-shell chromium
for b in chromium-1228 ffmpeg-1011; do test -d "$PREFIX/browsers/$b"; done
