# AgentSWE Builder runtime: Codex CLI 0.144.1 on Node 24.6.0 (Ubuntu 24.04).
# Used as (a) the Builder container of every Harbor Builder trial (Creation,
# Editing, Optimization) and (b) the image the runtime/judge brokers run in.
# Reproduces agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812
# without copying any host installation: Node comes from the official
# nodejs.org tarball, Codex from the public npm registry; both are checksum-pinned.
#
# Mirror hooks (all optional; defaults are the upstream sources):
#   DOCKER_REGISTRY  registry prefix for the base image (e.g. a Docker Hub mirror)
#   APT_SNAPSHOT     snapshot.ubuntu.com timestamp that pins apt package versions
#                    (set to "" to use the live archive)
#   APT_MIRROR       Ubuntu archive mirror URL; when set, the snapshot is not used
#   NODE_DIST_URL    Node.js distribution base URL
#   NPM_REGISTRY     npm registry URL
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ARG NODE_VERSION=24.6.0
ARG NODE_SHA256=352ddbc48b586c11f018ec9b886225117909ea93e05b4a04a6db32f3e63d0281
ARG CODEX_VERSION=0.144.1
ARG CODEX_PACKAGE_JSON_SHA256=e9756b0cb1e3a6f678ac9848365b6f3a22f11cede8348b883c2c05cb9c31705b
ARG CODEX_BINARY_SHA256=a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902
ARG APT_SNAPSHOT=20260813T110000Z
ARG APT_MIRROR=
ARG NODE_DIST_URL=https://nodejs.org/dist
ARG NPM_REGISTRY=https://registry.npmjs.org

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
       ca-certificates curl git python3 ripgrep; \
    cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig /etc/apt/apt.conf.d/99agentswe-snapshot; \
    rm -rf /var/lib/apt/lists/*

# Node.js: official linux-x64 distribution, unpacked as-is to /opt/node.
RUN set -eux; \
    curl -fsSL --retry 5 -o /tmp/node.tar.gz "${NODE_DIST_URL}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/node; \
    tar -xzf /tmp/node.tar.gz -C /opt/node --strip-components=1 --no-same-owner; \
    rm -f /tmp/node.tar.gz

# Codex CLI: the public npm package and its linux-x64 optional dependency,
# installed the way `npm install -g` lays them out, then moved to /opt/openai/codex.
RUN set -eux; \
    PATH=/opt/node/bin:$PATH npm install -g --prefix /tmp/codex-install --registry "${NPM_REGISTRY}" \
        --no-audit --no-fund --no-update-notifier "@openai/codex@${CODEX_VERSION}"; \
    mkdir -p /opt/openai; \
    mv /tmp/codex-install/lib/node_modules/@openai/codex /opt/openai/codex; \
    rm -rf /tmp/codex-install /root/.npm /root/.config /tmp/node-compile-cache; \
    echo "${CODEX_PACKAGE_JSON_SHA256}  /opt/openai/codex/package.json" | sha256sum -c -; \
    echo "${CODEX_BINARY_SHA256}  /opt/openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex" | sha256sum -c -

RUN ln -s /opt/node/bin/node /usr/local/bin/node \
    && ln -s /opt/node/bin/npm /usr/local/bin/npm \
    && ln -s /opt/node/bin/npx /usr/local/bin/npx \
    && ln -s /opt/openai/codex/bin/codex.js /usr/local/bin/codex \
    && test ! -e /root/.codex \
    && test ! -e /tmp/codex-home \
    && test ! -e /tmp/codex-secrets \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && test "$(node -p 'require("/opt/openai/codex/package.json").version')" = "${CODEX_VERSION}" \
    && test "$(codex --version)" = "codex-cli ${CODEX_VERSION}" \
    && rm -rf /root/.codex /tmp/node-compile-cache

LABEL org.opencontainers.image.title="AgentSWE Harbor Codex Builder Runtime" \
      org.opencontainers.image.description="Credential-free runtime for Harbor create-agent Builder trials" \
      io.agentswe.builder.codex.version="${CODEX_VERSION}" \
      io.agentswe.builder.node.version="${NODE_VERSION}" \
      io.agentswe.builder.credentials="runtime-only" \
      io.agentswe.builder.source.codex-package-sha256="${CODEX_PACKAGE_JSON_SHA256}"

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
