FROM ghcr.io/laude-institute/t-bench/python-3-13:20250620

# Evaluator-owned dependency layer. The task build still compiles the official
# main.c/validate.s and removes both sources exactly as upstream specifies.
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org/debian-security|$APT_MIRROR/debian-security|g; s|http://deb.debian.org/debian|$APT_MIRROR/debian|g" \
      /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y gcc make build-essential; \
    dpkg-query -W gcc make build-essential > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
