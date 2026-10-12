# Editing Code-axis judge image (Builder runtime + pure-Python requests stack at
# /opt/agentswe-code-deps).  Only the Code axis uses it; the Result-only protocol
# of the paper and of Lite does not start it.  Reproduces
# agentswe/edit-code-judge:0909-requests2342 without copying the host Harbor
# site-packages: the modules come from the pinned PyPI wheels.
ARG DOCKER_REGISTRY=docker.io
ARG BUILDER_IMAGE=agentswe-os/builder-codex:0.144.1-node24.6.0
FROM ${DOCKER_REGISTRY}/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS deps
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.lock vendor_pure.py expected-files.json /x/
RUN set -eux; \
    pip download --no-cache-dir --no-deps --require-hashes --only-binary=:all: --index-url "${PIP_INDEX_URL}" -d /x/w -r /x/requirements.lock; \
    python3 -I -B /x/vendor_pure.py /x/w /opt/agentswe-code-deps /x/expected-files.json

FROM ${BUILDER_IMAGE}
COPY --from=deps /opt/agentswe-code-deps /opt/agentswe-code-deps
ENV PYTHONPATH=/opt/agentswe-code-deps
ENV PYTHONDONTWRITEBYTECODE=1
RUN python3 -B -c "import requests,certifi,charset_normalizer,idna,urllib3; assert requests.__version__ == '2.34.2'; assert certifi.__version__ == '2026.07.22'; assert charset_normalizer.__version__ == '3.4.9'; assert idna.__version__ == '3.18'; assert urllib3.__version__ == '2.7.0'"
