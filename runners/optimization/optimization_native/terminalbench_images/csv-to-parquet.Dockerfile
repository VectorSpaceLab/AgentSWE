FROM ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624

# Evaluator-owned system dependency required by the official reference
# solution. Installation is moved out of scored task builds.
RUN apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl; \
    dpkg-query -W curl > /opt/agentswe-system-packages.txt; \
    rm -rf /var/lib/apt/lists/*
