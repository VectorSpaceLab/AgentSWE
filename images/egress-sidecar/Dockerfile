# Harbor 0.20.0 docker egress-control sidecar (gost transparent proxy +
# nftables policy), identical to harbor/environments/docker/
# harbor-docker-egress-control-sidecar/ except that the base image registry is
# a build argument.  Harbor builds this context itself and tags it
# harbor-prebuilt:harbor-docker-egress-control-sidecar--<content hash>; this copy
# lets CI prebuild/push it and lets hosts without Docker Hub access build it.
# Keep the context files byte-identical to third_party/harbor's patched copy.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/gogost/gost:3.2.7-nightly.20260602@sha256:afc0137758ab4ce399d47a299f9abbacbf522b52a17e59cbb4b4e7a1a66e9196

COPY gost.yaml allowlist.txt /opt/egress-sidecar/
COPY --chmod=755 entrypoint.sh /opt/egress-sidecar/entrypoint.sh
COPY --chmod=755 bin/network-policy /usr/local/bin/network-policy
