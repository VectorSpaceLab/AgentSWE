#!/bin/bash
# Playwright 1.61.0 browsers of the paper-era schema-guided-web-extraction environment
# (chromium_headless_shell-1228, ffmpeg-1011), installed from the Playwright CDN (mirror hook: PLAYWRIGHT_DOWNLOAD_HOST).
set -euo pipefail
: "${PREFIX:?}"
PLAYWRIGHT_BROWSERS_PATH="$PREFIX/.cache/ms-playwright" "$PREFIX/bin/python" -m playwright install --only-shell chromium
for b in chromium_headless_shell-1228 ffmpeg-1011; do test -d "$PREFIX/.cache/ms-playwright/$b"; done
