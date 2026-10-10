# TerminalBench evaluator-owned dependency layer for task accelerate-maximal-square
# (agentswe/tbench-accelerate-maximal-square-deps:20260813) on the legacy Python 3.6
# Taichi image (Docker Hub).
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/taichidev/taichi:v0.7.26@sha256:bec1d405e56468c6dd376c18657df699431b20d9064bb0976977faf1c66240b2
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN rm -f /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      vim tmux curl wget git python3-pip asciinema; \
    pip3 install --index-url "${PIP_INDEX_URL}" numpy==1.19.5 numba==0.53.1; \
    python3 -c "import llvmlite, numba, numpy, pytest; \
assert llvmlite.__version__ == '0.36.0'; \
assert numba.__version__ == '0.53.1'; \
assert numpy.__version__ == '1.19.5'; \
assert pytest.__version__ == '6.2.4'"; \
    dpkg-query -W vim tmux curl wget git python3-pip asciinema \
      > /opt/agentswe-system-packages.txt; \
    printf '%s\n' 'llvmlite==0.36.0' 'numba==0.53.1' 'numpy==1.19.5' \
      'pytest==6.2.4' > /opt/agentswe-python-packages.txt; \
    rm -rf /var/lib/apt/lists/*
