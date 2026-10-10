FROM taichidev/taichi:v0.7.26

# Evaluator-owned dependency layer for the legacy Python 3.6 Taichi image.
# The official task's package setup is moved out of scored trials; the
# resulting image ID is locked by terminalbench_controller.py.
RUN rm -f /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      vim tmux curl wget git python3-pip asciinema; \
    pip3 install numpy==1.19.5 numba==0.53.1; \
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
