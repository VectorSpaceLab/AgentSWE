# LibreOffice root of the document-to-editable-pptx evaluator renderer (AGENTSWE_LIBREOFFICE_ROOT, mounted read-only).
# The paper root (create75 dependencies/libreoffice, 721 MB) is the official TDF 25.8.7.3 deb build unpacked with
# dpkg-deb -x, plus Ubuntu jammy libxinerama1 2:1.1.4-3.  Both inputs are images.json "downloads" (sha256-pinned,
# fetched on the host by setup) and appear here as downloads/.  The tarball pin equals TDF's published
# LibreOffice_25.8.7.3_Linux_x86-64_deb.tar.gz.sha256.  Export with setup; the result is a plain directory.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea AS unpack
COPY downloads/ /downloads/
RUN set -eux; \
    mkdir -p /tmp/lo /root-out; \
    tar -xzf /downloads/LibreOffice_25.8.7.3_Linux_x86-64_deb.tar.gz -C /tmp/lo; \
    find /tmp/lo -name '*.deb' | sort | while read -r deb; do dpkg-deb -x "$deb" /root-out; done; \
    dpkg-deb -x /downloads/libxinerama1_1.1.4-3_amd64.deb /root-out; \
    test -x /root-out/opt/libreoffice25.8/program/soffice; \
    rm -rf /tmp/lo

FROM scratch
COPY --from=unpack /root-out /libreoffice-root
