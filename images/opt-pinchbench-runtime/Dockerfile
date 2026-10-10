# PinchBench runtime: OpenClaw 2026.7.1-2 (npm ci from the pinned lockfile) on
# Node 24.19.0 at /openclaw and /node, the PinchBench skill repository at
# /pinchbench, and PyYAML 6.0.2 for Python 3.12 at /yaml-site, on top of
# opt-python312 (/python).  Mount points are the ones the controller used for
# host bind mounts, so the runtime/grader entrypoints are unchanged.
# Sources: nodejs.org; npm registry (openclaw, MIT); https://github.com/pinchbench/skill (MIT) @ 819384a;
# PyPI (PyYAML, MIT).  Mirror hooks: NODE_DIST_URL, NPM_REGISTRY, PIP_INDEX_URL, GITHUB_CODELOAD.
ARG DOCKER_REGISTRY=docker.io
ARG BASE_IMAGE=agentswe-os/opt-python312:3.12.13-20260807

FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS fetch
ARG NODE_VERSION=24.19.0
ARG NODE_SHA256=f625d97cd707df4ff96254916fbc5ff014f09c09effe5a1e0ca8f6d41a8789d4
ARG NODE_DIST_URL=https://nodejs.org/dist
ARG PINCHBENCH_COMMIT=819384ae830492365b8363fc26bc2602e73f216d
ARG PINCHBENCH_TARBALL_SHA256=
ARG GITHUB_CODELOAD=https://codeload.github.com
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG APT_MIRROR=
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources; fi; \
    apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL --retry 5 -o /tmp/node.tar.gz "${NODE_DIST_URL}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; \
    mkdir -p /node; tar -xzf /tmp/node.tar.gz -C /node --strip-components=1 --no-same-owner; rm -f /tmp/node.tar.gz; \
    curl -fsSL --retry 5 -o /tmp/pinch.tar.gz "${GITHUB_CODELOAD}/pinchbench/skill/tar.gz/${PINCHBENCH_COMMIT}"; \
    if [ -n "$PINCHBENCH_TARBALL_SHA256" ]; then echo "${PINCHBENCH_TARBALL_SHA256}  /tmp/pinch.tar.gz" | sha256sum -c -; fi; \
    mkdir -p /pinchbench; tar -xzf /tmp/pinch.tar.gz -C /pinchbench --strip-components=1 --no-same-owner; rm -f /tmp/pinch.tar.gz

FROM fetch AS openclaw
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY openclaw/package.json openclaw/package-lock.json /openclaw/
RUN set -eux; \
    cd /openclaw; \
    PATH=/node/bin:$PATH npm ci --registry "${NPM_REGISTRY}" --no-audit --no-fund --no-update-notifier; \
    rm -rf /root/.npm /tmp/node-compile-cache; \
    PATH=/node/bin:$PATH node /openclaw/node_modules/openclaw/openclaw.mjs agent --help | grep -q -- "--local"; \
    rm -rf /tmp/node-compile-cache

FROM ${BASE_IMAGE} AS yaml
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PYYAML_WHEEL_SHA256=80bab7bfc629882493af4aa31a4cfa43a4c57c83813253626916b8c7ada83476
RUN set -eux; \
    /python/bin/python3 -m pip download --no-cache-dir --no-deps --only-binary=:all: --index-url "${PIP_INDEX_URL}" -d /tmp/w "PyYAML==6.0.2"; \
    echo "${PYYAML_WHEEL_SHA256}  $(ls /tmp/w/PyYAML-6.0.2-cp312-*.whl)" | sha256sum -c -; \
    /python/bin/python3 -m pip install --no-cache-dir --no-deps --no-index --target /yaml-site /tmp/w/PyYAML-6.0.2-cp312-*.whl; \
    find /yaml-site -name __pycache__ -prune -exec rm -rf {} +

FROM ${BASE_IMAGE}
COPY --from=fetch /node /node
COPY --from=fetch /pinchbench /pinchbench
COPY --from=openclaw /openclaw /openclaw
COPY --from=yaml /yaml-site /yaml-site
ENV PATH=/node/bin:/openclaw/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CMD ["/python/bin/python3"]
