# AI-Scientist "compiler runtime": the file subset of edit-candidate-python311
# that agentloop/candidate_adapter.py mounts at /compiler inside bubblewrap.
# Export without a container:
#   docker buildx build -f compiler-runtime.Dockerfile \
#     --output type=local,dest=$AGENTSWE_HOME/envs/ai-python311-compiler-runtime .
ARG PY311_IMAGE=agentswe/edit-candidate-python311:3.11.16
FROM ${PY311_IMAGE} AS src
COPY compiler-runtime.manifest.json extract_compiler_runtime.py /x/
RUN python3 -I -B /x/extract_compiler_runtime.py /x/compiler-runtime.manifest.json /export

FROM scratch
COPY --from=src /export /
