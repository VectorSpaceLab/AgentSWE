# Legacy (0826 Harbor-era Editing): Builder runtime + iproute2, for evaluators
# that run `unshare … ip link set lo up`.  No current (agentloop) task tree
# references it; kept so historical configurations can be rebuilt.
# Reproduces agentswe/edit-candidate-runtime:0826.
ARG BUILDER_IMAGE=agentswe-os/builder-codex:0.144.1-node24.6.0
FROM ${BUILDER_IMAGE}

ARG APT_SNAPSHOT=20260827T230000Z
ARG APT_MIRROR=

RUN set -eux; \
    src=/etc/apt/sources.list.d/ubuntu.sources; cp "$src" /tmp/ubuntu.sources.orig; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/\?|${APT_MIRROR}|g" "$src"; \
    elif [ -n "$APT_SNAPSHOT" ]; then \
      printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-backports noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$APT_SNAPSHOT" > "$src"; \
    fi; \
    apt-get -o Acquire::Retries=8 update; \
    test -n "$(ls /var/lib/apt/lists/*InRelease 2>/dev/null)"; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2; \
    cp /tmp/ubuntu.sources.orig "$src"; rm -f /tmp/ubuntu.sources.orig; \
    rm -rf /var/lib/apt/lists/*
