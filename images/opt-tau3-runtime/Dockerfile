# tau3 (retail) runtime: tau2-bench at the pinned commit with its uv-locked
# virtualenv at /tau2/.venv, on top of opt-python312 (/python).
# The controller runs `/python/bin/python3 /runtime.py` with
# PYTHONPATH=/tau2/src:/tau2/.venv/lib/python3.12/site-packages; the host
# mounts of the tau2-bench snapshot and of the host Python are no longer needed.
# Source: https://github.com/sierra-research/tau2-bench (MIT) @ 668d3bc.
# The default fetch path verifies the GitHub tarball sha256.  Offline or mirror
# builds can replace the `tau2src` stage with a local checkout of the same
# commit:  docker buildx build --build-context tau2src=<checkout dir> ...
ARG DOCKER_REGISTRY=docker.io
ARG BASE_IMAGE=agentswe-os/opt-python312:3.12.13-20260807

FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS tau2src
ARG TAU2_COMMIT=668d3bcd135c02aa3438f987ef45735b7c163ee3
ARG TAU2_TARBALL_SHA256=813209f1eb87e61c1a4cc4f0de549d471ba207a0bb6442990e193c5ca0e98809
ARG GITHUB_CODELOAD=https://codeload.github.com
ARG APT_MIRROR=
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources; fi; \
    apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL --retry 5 -o /tmp/tau2.tar.gz "${GITHUB_CODELOAD}/sierra-research/tau2-bench/tar.gz/${TAU2_COMMIT}"; \
    echo "${TAU2_TARBALL_SHA256}  /tmp/tau2.tar.gz" | sha256sum -c -; \
    mkdir -p /src; tar -xzf /tmp/tau2.tar.gz -C /src --strip-components=1 --no-same-owner; \
    rm -f /tmp/tau2.tar.gz

FROM ${BASE_IMAGE} AS venv
ARG UV_VERSION=0.12.3
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY --from=tau2src /src /tau2
# uv.lock is the source of truth; it is exported to a hash-pinned requirements
# file so that PIP_INDEX_URL mirrors can serve the same files.
RUN set -eux; \
    /python/bin/python3 -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --target /opt/uv "uv==${UV_VERSION}"; \
    export UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 UV_LINK_MODE=copy; \
    cd /tau2; \
    /opt/uv/bin/uv export --frozen --no-dev --no-emit-project --no-header --format requirements-txt -o /tmp/tau2-requirements.txt; \
    /opt/uv/bin/uv venv --python /python/bin/python3 --prompt tau2 /tau2/.venv; \
    /opt/uv/bin/uv pip install --python /tau2/.venv/bin/python --no-deps --require-hashes --index-url "${PIP_INDEX_URL}" -r /tmp/tau2-requirements.txt; \
    /opt/uv/bin/uv pip install --python /tau2/.venv/bin/python --no-deps --index-url "${PIP_INDEX_URL}" --editable /tau2; \
    test "$(ls -d /tau2/.venv/lib/python3.12/site-packages/*.dist-info | wc -l)" -ge 75; \
    mkdir -p /tau2/data/simulations; \
    rm -f /tmp/tau2-requirements.txt; \
    find /tau2/.venv -name __pycache__ -prune -exec rm -rf {} +

FROM ${BASE_IMAGE}
COPY --from=venv /tau2 /tau2
ENV PYTHONPATH=/tau2/src:/tau2/.venv/lib/python3.12/site-packages
CMD ["/python/bin/python3"]
