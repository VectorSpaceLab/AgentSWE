# Editing lower-agent ("candidate") image: official python:3.11 (3.11.16,
# Debian trixie) plus the Harbor log directories.  Used by Claude, Aider,
# AI-Scientist and Dyad (lower agent + presubmit syntax check); AI-Scientist's
# bwrap "compiler runtime" is a file subset of this image (see
# tasks/editing/ai-scientist-reproducibility-gate/env/).
# Reproduces agentswe/edit-candidate-python311:0826.
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/python@sha256:e087dbafe32f3a19e90616cea7a5c11827373e7eec3fc9842f003fb07b938669

# Harbor redirects verifier stdout before test.sh starts, so the convention
# directory must already exist in images that do not inherit the Builder
# runtime layout.
RUN mkdir -p /logs/verifier /logs/agent /workspace
WORKDIR /workspace
