# TerminalBench evaluator-owned dependency layer for task write-compressor
# (agentswe/tbench-write-compressor-deps:20260812).  Moves apt installation out of
# scored trials; task assets/tests stay upstream.  The controller pins the image
# id of the historical build; a rebuild installs the then-current Ubuntu packages,
# so either pin APT_SNAPSHOT or publish the archived historical image.
# Index digest; its linux/amd64 config is the image id terminalbench_controller.py pins.
ARG GHCR_REGISTRY=ghcr.io
FROM ${GHCR_REGISTRY}/laude-institute/t-bench/ubuntu-24-04:20250624@sha256:d34916434f1304da8b1fc7aeb547d1bee53770a4c4fe0ff4637eaca293d648d5
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" \
      /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y gcc rustc bc; \
    dpkg-query -W gcc rustc bc > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
