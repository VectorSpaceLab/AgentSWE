# Evaluator-only browser and shared-library bundles used by the Creation verifiers:
#   /runtime-dependencies-v1  browser/lib (Chromium's shared libraries), pptx/lib
#                             (LibreOffice/PPTX renderer libraries), fonts/, manifest.json
#   /trusted-browser-v1       chrome/ (Chrome for Testing 149.0.7827.55 = Playwright
#                             chromium-1228), node (Node 22.22.2), lib/, fonts/,
#                             bundle-manifest.json; mounted at /opt/agentswe-trusted-browser
# Replaces directories that were copied from host installations.  Export with
#   docker buildx build --output type=local,dest=$AGENTSWE_HOME/deps images/trusted-browser
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea AS libs
ARG APT_SNAPSHOT=20260910T000000Z
ARG APT_MIRROR=
RUN set -eux; \
    src=/etc/apt/sources.list.d/ubuntu.sources; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" "$src"; \
    elif [ -n "$APT_SNAPSHOT" ]; then \
      printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-backports noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$APT_SNAPSHOT" > "$src"; \
      echo 'Acquire::https::snapshot.ubuntu.com::Verify-Peer "false";' > /etc/apt/apt.conf.d/99agentswe-snapshot; \
    fi; \
    apt-get -o Acquire::Retries=8 update; \
    test -n "$(ls /var/lib/apt/lists/*InRelease 2>/dev/null)"; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libasound2t64=1.2.11-1ubuntu0.3 libatk-bridge2.0-0t64=2.52.0-1build1 libatk1.0-0t64=2.52.0-1build1 \
      libatspi2.0-0t64=2.52.0-1build1 libcairo2=1.18.0-3build1 libcups2t64=2.4.7-1.2ubuntu7.14 \
      libdbus-1-3=1.14.10-4ubuntu4.1 libgbm1=25.2.8-0ubuntu0.24.04.2 libglib2.0-0t64=2.80.0-6ubuntu3.8 \
      libnspr4=2:4.35-1.1build1 libnss3=2:3.98-1ubuntu0.2 libpango-1.0-0=1.52.1+ds-1build1 \
      libx11-6=2:1.8.7-1build1 libxcb1=1.15-1ubuntu2 libxcomposite1=1:0.4.5-1build3 libxdamage1=1:1.1.6-1build1 \
      libxext6=2:1.3.4-1build2 libxfixes3=1:6.0.0-2build1 libxkbcommon0=1.6.0-1build1 libxrandr2=2:1.5.2-2build1 \
      python3=3.12.3-0ubuntu2.1 fontconfig-config fonts-dejavu-core ca-certificates curl unzip; \
    rm -rf /var/lib/apt/lists/*

# Fonts: the paper-era bundle copied fontconfig 2.13.1 configuration and the DejaVu
# fonts of the Ubuntu 22.04 evaluation host, so they come from a jammy stage.
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02 AS fonts
ARG APT_SNAPSHOT=20260910T000000Z
ARG APT_MIRROR=
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" /etc/apt/sources.list; \
    elif [ -n "$APT_SNAPSHOT" ]; then \
      printf 'deb https://snapshot.ubuntu.com/ubuntu/%s jammy main restricted universe multiverse\ndeb https://snapshot.ubuntu.com/ubuntu/%s jammy-updates main restricted universe multiverse\ndeb https://snapshot.ubuntu.com/ubuntu/%s jammy-security main restricted universe multiverse\n' "$APT_SNAPSHOT" "$APT_SNAPSHOT" "$APT_SNAPSHOT" > /etc/apt/sources.list; \
      echo 'Acquire::https::snapshot.ubuntu.com::Verify-Peer "false";' > /etc/apt/apt.conf.d/99agentswe-snapshot; \
    fi; \
    apt-get -o Acquire::Retries=8 update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      fontconfig-config fonts-dejavu-core fonts-dejavu-extra language-selector-common; \
    rm -rf /var/lib/apt/lists/*

FROM libs AS assemble
COPY --from=fonts /etc/fonts /fonts/etc
COPY --from=fonts /usr/share/fonts /fonts/share
ARG CHROME_VERSION=149.0.7827.55
ARG CHROME_ZIP_SHA256=13113b963ac22fffdad898a677591028e4397c46c1daa9e61811258eed6e35b5
ARG CFT_BASE_URL=https://storage.googleapis.com/chrome-for-testing-public
ARG NODE_VERSION=22.22.2
ARG NODE_SHA256=978978a635eef872fa68beae09f0aad0bbbae6757e444da80b570964a97e62a3
ARG NODE_DIST_URL=https://nodejs.org/dist
COPY runtime-dependencies.manifest.json fonts.manifest.json assemble.py /x/
RUN set -eux; \
    curl -fsSL --retry 5 -o /tmp/chrome.zip "${CFT_BASE_URL}/${CHROME_VERSION}/linux64/chrome-linux64.zip"; \
    echo "${CHROME_ZIP_SHA256}  /tmp/chrome.zip" | sha256sum -c -; \
    unzip -q /tmp/chrome.zip -d /tmp; \
    curl -fsSL --retry 5 -o /tmp/node.tar.gz "${NODE_DIST_URL}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    if [ -n "$NODE_SHA256" ]; then echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c -; fi; \
    mkdir -p /tmp/node; tar -xzf /tmp/node.tar.gz -C /tmp/node --strip-components=1; \
    python3 -I -B /x/assemble.py /x/runtime-dependencies.manifest.json /out/runtime-dependencies-v1 /out/trusted-browser-v1 \
        /tmp/chrome-linux64 /tmp/node/bin/node "v${NODE_VERSION}" /x/fonts.manifest.json

FROM scratch
COPY --from=assemble /out /
