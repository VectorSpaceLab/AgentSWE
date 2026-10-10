# Build one environment and export it as a plain directory, without creating a
# container:
#   docker buildx build -f images/env-builder/env.Dockerfile \
#     --build-arg ENV_NAME=<name> --build-context spec=tasks/<family>/<task>/env \
#     --output type=local,dest=$AGENTSWE_HOME/envs/<name> images/env-builder
# (`docker run` of agentswe/env-builder with the env dir bind-mounted at
#  /opt/agentswe/benchmark/envs/<name> is the equivalent fallback.)
# ENV_BUILDER is the image named by env.json "builder_image" (agentswe/env-builder
# for conda/uv envs; agentswe/edit-candidate-python311 for the Python 3.11 overlays).
ARG ENV_BUILDER=agentswe-os/env-builder:1
FROM ${ENV_BUILDER} AS build
ARG ENV_NAME
# Mirror hooks (exported to the build step as environment variables):
ARG CONDA_FORGE_URL=https://conda.anaconda.org/conda-forge
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ARG UBUNTU_ARCHIVE_URL=http://archive.ubuntu.com/ubuntu
# Release assets fetched on the host by setup (env.json "downloads"), handed over as file:///spec/downloads/...
ARG LEAN_RELEASE_URL=
COPY agentswe-build-env /usr/local/bin/agentswe-build-env
COPY --from=spec . /spec
RUN agentswe-build-env /spec "/opt/agentswe/benchmark/envs/${ENV_NAME}"

FROM scratch
ARG ENV_NAME
COPY --from=build /opt/agentswe/benchmark/envs/${ENV_NAME} /
