# Builder for the trusted task environments ("envs") that adapters mount at
# /opt/agentswe/benchmark/envs/<name>.  Environments are created *at that path*
# inside this image, so conda prefix rewriting and venv shebangs match the
# in-container mount point; `agentswe setup` exports the result to
# $AGENTSWE_HOME/envs/<name> (see env.Dockerfile).
# Tools: micromamba (conda envs from @EXPLICIT specs), uv (venvs / Python
# installs), Node 24.6.0 + npm (npm-packed assets), curl, dpkg-deb.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ARG APT_SNAPSHOT=20260901T180000Z
ARG APT_MIRROR=
ARG MICROMAMBA_VERSION=2.8.1
ARG MICROMAMBA_BUILD=1
ARG MICROMAMBA_SHA256=8528263837623551a44464a372e5bd6b0b856479a83d2a77490a19dd98da3b06
ARG CONDA_FORGE_URL=https://conda.anaconda.org/conda-forge
ARG NODE_VERSION=24.6.0
ARG NODE_SHA256=352ddbc48b586c11f018ec9b886225117909ea93e05b4a04a6db32f3e63d0281
ARG NODE_DIST_URL=https://nodejs.org/dist
ARG UV_VERSION=0.12.3
ARG UV_WHEEL_SHA256=1482d1462b1aecd18ee33627363fe1c63d6a194f12d40d37efc446d9e0d800a1
ARG PIP_INDEX_URL=https://pypi.org/simple

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
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 xz-utils zstd git python3 unzip; \
    cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig /etc/apt/apt.conf.d/99agentswe-snapshot; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL --retry 5 -o /tmp/mm.tar.bz2 "${CONDA_FORGE_URL}/linux-64/micromamba-${MICROMAMBA_VERSION}-${MICROMAMBA_BUILD}.tar.bz2"; \
    echo "${MICROMAMBA_SHA256}  /tmp/mm.tar.bz2" | sha256sum -c -; \
    tar -xjf /tmp/mm.tar.bz2 -C /usr/local bin/micromamba; rm -f /tmp/mm.tar.bz2; \
    micromamba --version; \
    curl -fsSL --retry 5 -o /tmp/node.tar.gz "${NODE_DIST_URL}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/node; tar -xzf /tmp/node.tar.gz -C /opt/node --strip-components=1 --no-same-owner; rm -f /tmp/node.tar.gz; \
    python3 -c "import re,sys,urllib.parse,urllib.request; idx=sys.argv[1].rstrip('/')+'/uv/'; html=urllib.request.urlopen(idx, timeout=60).read().decode(); m=re.search(r'href=\"([^\"]*uv-'+sys.argv[2]+r'-py3-none-manylinux_2_17_x86_64[^\"#]*\\.whl)#sha256='+sys.argv[3], html); print(urllib.parse.urljoin(idx, m.group(1)))" "${PIP_INDEX_URL}" "${UV_VERSION}" "${UV_WHEEL_SHA256}" > /tmp/uv.url; \
    mkdir -p /tmp/uvw; curl -fsSL --retry 5 -o /tmp/uvw/uv.whl "$(cat /tmp/uv.url)"; echo "${UV_WHEEL_SHA256}  /tmp/uvw/uv.whl" | sha256sum -c -; \
    unzip -q -o /tmp/uvw/uv.whl -d /tmp/uvw; install -m 0755 /tmp/uvw/uv-${UV_VERSION}.data/scripts/uv /usr/local/bin/uv; rm -rf /tmp/uvw /tmp/uv.url; \
    uv --version

ENV PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    MAMBA_ROOT_PREFIX=/opt/mamba \
    CONDA_FORGE_URL=${CONDA_FORGE_URL} \
    PIP_INDEX_URL=${PIP_INDEX_URL}
COPY agentswe-build-env /usr/local/bin/agentswe-build-env
RUN chmod 0755 /usr/local/bin/agentswe-build-env
WORKDIR /opt/agentswe/benchmark/envs
