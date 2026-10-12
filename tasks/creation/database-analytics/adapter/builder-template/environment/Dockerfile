ARG AGENTSWE_BUILDER_BASE=agentswe-os/builder-codex:0.144.1-node24.6.0
FROM ${AGENTSWE_BUILDER_BASE}

COPY submit_dev_candidate /usr/local/bin/submit_dev_candidate
RUN chmod 0755 /usr/local/bin/submit_dev_candidate

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
