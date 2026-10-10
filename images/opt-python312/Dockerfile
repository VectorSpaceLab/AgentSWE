# Optimization per-case base: Ubuntu 24.04 + python-build-standalone CPython
# 3.12.13 (release 20260807, install_only_stripped -- the exact build uv 0.12.3
# installed as the harness Python) at /python, plus the CA bundle.
# Replaces `docker run ubuntu:24.04 -v <host python>:/python:ro -v /etc/ssl/certs:/etc/ssl/certs:ro`
# used by the tau3 / PinchBench brokers, runtimes and graders.
# Mirror hooks: DOCKER_REGISTRY, APT_SNAPSHOT / APT_MIRROR, PBS_BASE_URL
# (e.g. a mirror of the python-build-standalone release assets).
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS base

ARG APT_SNAPSHOT=20260930T000000Z
ARG APT_MIRROR=
RUN set -eux; \
    src=/etc/apt/sources.list.d/ubuntu.sources; cp "$src" /tmp/ubuntu.sources.orig; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" "$src"; \
    elif [ -n "$APT_SNAPSHOT" ]; then \
      printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-backports noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$APT_SNAPSHOT" > "$src"; \
      echo 'Acquire::https::snapshot.ubuntu.com::Verify-Peer "false";' > /etc/apt/apt.conf.d/99agentswe-snapshot; \
    fi; \
    apt-get -o Acquire::Retries=8 update; \
    test -n "$(ls /var/lib/apt/lists/*InRelease 2>/dev/null)"; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates; \
    cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig /etc/apt/apt.conf.d/99agentswe-snapshot; \
    rm -rf /var/lib/apt/lists/*

FROM base AS fetch
ARG APT_SNAPSHOT=20260930T000000Z
ARG PBS_BASE_URL=https://github.com/astral-sh/python-build-standalone/releases/download
ARG PBS_RELEASE=20260807
ARG PYTHON_VERSION=3.12.13
ARG PBS_SHA256=506191be3ee7bd190a8834dcdc1b3bc70aab50608deccc711935aa007239cabd
RUN set -eux; \
    apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl; \
    curl -fsSL --retry 5 -o /tmp/python.tar.gz \
      "${PBS_BASE_URL}/${PBS_RELEASE}/cpython-${PYTHON_VERSION}%2B${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"; \
    echo "${PBS_SHA256}  /tmp/python.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/python.tar.gz -C / --no-same-owner; \
    test "$(/python/bin/python3 -c 'import sys; print(sys.version.split()[0])')" = "${PYTHON_VERSION}"; \
    find /python -name __pycache__ -prune -exec rm -rf {} +

FROM base
COPY --from=fetch /python /python
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
CMD ["/python/bin/python3"]
