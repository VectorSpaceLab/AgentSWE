FROM agentswe-os/ubuntu-base:24.04-561618e2-git

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY . /tests

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
