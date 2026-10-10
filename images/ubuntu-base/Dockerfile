# Bare Ubuntu 24.04 (the digest every Creation/Optimization template pins) plus
# git.  Harbor builds the per-trial verifier/eval images (`COPY . /tests`) on top
# of this local tag, so those Dockerfiles no longer name a registry, and the host
# bind mounts of /usr/bin/git, /usr/lib/git-core and /usr/share/git-core go away.
# INSTALL_GIT=0 gives a byte-identical alias of the pinned base.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ARG APT_SNAPSHOT=20260901T180000Z
ARG APT_MIRROR=
ARG INSTALL_GIT=1

RUN set -eux; \
    if [ "$INSTALL_GIT" = 1 ]; then \
      src=/etc/apt/sources.list.d/ubuntu.sources; cp "$src" /tmp/ubuntu.sources.orig; \
      if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" "$src"; \
      elif [ -n "$APT_SNAPSHOT" ]; then \
        printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-backports noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$APT_SNAPSHOT" > "$src"; \
        echo 'Acquire::https::snapshot.ubuntu.com::Verify-Peer "false";' > /etc/apt/apt.conf.d/99agentswe-snapshot; \
      fi; \
      apt-get -o Acquire::Retries=8 update; \
      test -n "$(ls /var/lib/apt/lists/*InRelease 2>/dev/null)"; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git; \
      cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig /etc/apt/apt.conf.d/99agentswe-snapshot; \
      rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
