FROM docker.io/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN python -m pip install --no-cache-dir uv==0.8.14
WORKDIR /opt/osworld
# Build with the pinned OSWorld source tree as context. Its pyproject, uv.lock,
# and pinned submodules are copied together so the local agp-client source is present.
COPY . /opt/osworld
RUN test "$(sha256sum uv.lock | cut -d' ' -f1)" = \
    "123c8837f7901b6b1aa34836f49e00fcca0f7e77de6f4839bd9cd6fa2ea4f44e" \
    && uv sync --frozen --no-dev

ENTRYPOINT ["/opt/osworld/.venv/bin/python"]
