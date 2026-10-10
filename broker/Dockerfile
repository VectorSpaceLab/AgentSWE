# AgentSWE unified provider broker (standard library only, no pip install).
# Registry prefix is a build arg; the digest pins the content (layout contract 0.6).
# Digest = OCI index of python:3.12-slim-bookworm, resolved 2026-10-01 through a registry mirror
# (content-addressed, identical on docker.io).
ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/agentswe/broker
COPY agentswe_broker/ ./agentswe_broker/
RUN python -c "import agentswe_broker.server, agentswe_broker.config"
# Keys arrive as a read-only 0600 file (--credential-file).  The runner starts the
# container with --user <host uid>:<host gid> so that file stays 0600 and readable;
# no USER is baked in for that reason.  Ledgers/stats go to a mounted run directory.
ENTRYPOINT ["python", "-m", "agentswe_broker"]
CMD ["--help"]
