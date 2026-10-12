# TerminalBench evaluator-owned dependency layer for task csv-to-parquet
# (agentswe/tbench-csv-to-parquet-deps:20260813).
# Index digest; its linux/amd64 config is the image id terminalbench_controller.py pins.
ARG GHCR_REGISTRY=ghcr.io
FROM ${GHCR_REGISTRY}/laude-institute/t-bench/ubuntu-24-04:20250624@sha256:d34916434f1304da8b1fc7aeb547d1bee53770a4c4fe0ff4637eaca293d648d5
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" \
      /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl; \
    dpkg-query -W curl > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
