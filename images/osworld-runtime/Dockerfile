# OSWorld evaluator runtime (Optimization / OSWorld, tier T3).  Python 3.12.11 slim +
# the pinned OSWorld source (xlang-ai/OSWorld @ 091f5ef, Apache-2.0, with its
# submodules) + its uv.lock.  The VM (Ubuntu.qcow2, 24.5 GB, from the HF dataset
# xlangai/ubuntu_osworld @ a5d9c3e) and the KVM provider image
# (happysixd/osworld-docker@sha256:0e6497a9…) are fetched separately by setup and
# are not redistributed.  Runtime needs /dev/kvm, /dev/net/tun, /dev/vhost-net and
# host networking for the provider; Builder and Candidate jobs get none of them.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS src
ARG OSWORLD_REPO=https://github.com/xlang-ai/OSWorld.git
ARG OSWORLD_COMMIT=091f5ef1d5544bc74953c77875d5feb5bed30108
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none "${OSWORLD_REPO}" /opt/osworld && cd /opt/osworld \
    && git checkout "${OSWORLD_COMMIT}" && git submodule update --init --recursive \
    && rm -rf .git

FROM ${DOCKER_REGISTRY}/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy
RUN python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" uv==0.8.14
WORKDIR /opt/osworld
COPY --from=src /opt/osworld /opt/osworld
RUN test "$(sha256sum uv.lock | cut -d' ' -f1)" = \
    "123c8837f7901b6b1aa34836f49e00fcca0f7e77de6f4839bd9cd6fa2ea4f44e" \
    && uv sync --frozen --no-dev

ENTRYPOINT ["/opt/osworld/.venv/bin/python"]
