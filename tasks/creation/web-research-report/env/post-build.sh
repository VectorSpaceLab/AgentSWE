#!/bin/bash
# Playwright 1.61.0 browsers of the paper-era web-research-report environment
# (chromium-1228, chromium_headless_shell-1228, ffmpeg-1011), installed from the Playwright CDN (mirror hook: PLAYWRIGHT_DOWNLOAD_HOST).
set -euo pipefail
: "${PREFIX:?}"
PLAYWRIGHT_BROWSERS_PATH="$PREFIX/playwright-browsers" "$PREFIX/bin/python" -m playwright install  chromium
for b in chromium-1228 chromium_headless_shell-1228 ffmpeg-1011; do test -d "$PREFIX/playwright-browsers/$b"; done
