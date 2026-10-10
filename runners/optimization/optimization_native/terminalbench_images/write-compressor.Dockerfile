FROM ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624

# Evaluator-owned dependency layer. Package installation is moved out of each
# scored trial; the resulting image ID is locked by terminalbench_controller.py.
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://archive.ubuntu.com/ubuntu|$APT_MIRROR/ubuntu|g; s|http://security.ubuntu.com/ubuntu|$APT_MIRROR/ubuntu|g" \
      /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y gcc rustc bc; \
    dpkg-query -W gcc rustc bc > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
