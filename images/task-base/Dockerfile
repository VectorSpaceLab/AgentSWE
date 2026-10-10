# Thin per-case image for Creation candidates (Harbor task-template environment).
# Replaces the per-trial `FROM <mirror>/library/ubuntu@sha256:5616…`
# + `apt-get install python3 ca-certificates` and the host bind mounts of
# /usr/bin/git, /usr/lib/git-core, /usr/share/git-core (git is installed here).
# APT_SNAPSHOT pins the package versions of the paper-era build (2026-09-01);
# APT_MIRROR switches to a mirror (live versions).  INSTALL_EXTRA adds packages
# (Authorized vulnerability validation needs "iptables nftables").
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ARG APT_SNAPSHOT=20260901T180000Z
ARG APT_MIRROR=
ARG INSTALL_GIT=1
ARG INSTALL_EXTRA=

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
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates \
        $( [ "$INSTALL_GIT" = 1 ] && echo git ) ${INSTALL_EXTRA}; \
    cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig /etc/apt/apt.conf.d/99agentswe-snapshot; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
