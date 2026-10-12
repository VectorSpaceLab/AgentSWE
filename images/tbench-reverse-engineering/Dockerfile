# TerminalBench evaluator-owned dependency layer for task reverse-engineering
# (agentswe/tbench-reverse-engineering-deps:20260812).  The task build still compiles
# the official main.c/validate.s and removes both sources as upstream specifies.
# Index digest; its linux/amd64 config is the image id terminalbench_controller.py pins.
ARG GHCR_REGISTRY=ghcr.io
FROM ${GHCR_REGISTRY}/laude-institute/t-bench/python-3-13:20250620@sha256:236734f0cafcce942ca09316d57236c2273a2b5411e116454a22cf6d718d95f5
ARG DEBIAN_MIRROR=
RUN if [ -n "$DEBIAN_MIRROR" ]; then sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_MIRROR}-security|g; s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y gcc make build-essential; \
    dpkg-query -W gcc make build-essential > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
