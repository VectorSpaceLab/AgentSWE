# Available resources

## Local runtime

- Python 3.12 or later, Git, Bash, and the supplied repository.
- The repository's focused pytest dependencies are available to the
  evaluator.
- Read/write access to the writable repository copy, case projects, declared
  stores, and the designated submission/output directory.
- Deterministic synthetic Markdown papers, small Python projects, small local
  datasets, and the public development cases.
- A 600-second timeout per process with ample process-tree RSS headroom. Up
  to eight tiny product processes may run concurrently. No virtual address
  space limit, GPU, container daemon, or remote runtime is required.

All patched-product fixture execution is offline, with a remote call budget
of zero.

The issue evidence for this improvement is public upstream material:
[DeepCode #132](https://github.com/HKUDS/DeepCode/issues/132) (AGENTS.md
prompt injection), [#128](https://github.com/HKUDS/DeepCode/issues/128)
(shell injection through the command tool), and
[PR #164](https://github.com/HKUDS/DeepCode/pull/164) (fail-safe private
file recovery). The abstraction is an offline, auditable, reversible security
boundary rather than a GUI or network service.

## Optional builder assistance

During implementation only, the authorized services are:

- GATEWAY Responses API: `POST https://gateway.example.com/v1/responses`,
  exact model `gpt-5.6-sol`, bearer token from `GATEWAY_API_KEY`, reasoning
  effort `high`.
- Serper-compatible search: `POST https://search.example.com/serp_search_v1`,
  token from `SERPER_TOKEN`.
- Public web retrieval through the environment's normal retrieval facility.
  Use the proxy `127.0.0.1:7896` when routing is required.

For GATEWAY send `Authorization: Bearer $GATEWAY_API_KEY`,
`Content-Type: application/json`, and a Responses-compatible body containing
`model: "gpt-5.6-sol"` and `reasoning: {"effort": "high"}`. For Serper send
`Content-Type: application/json` and the body
`{"query": "<public-web query>", "token": "$SERPER_TOKEN"}`. Never put
credentials in URLs, source files, patches, reports, or output.

Per builder run, the maximum optional assistance budget is 30 GATEWAY
calls, 20 Serper calls, and 20 ordinary web retrieval calls. No other model
or search provider is authorized. Do not send repository dumps, evaluation
material, hidden cases, credentials, private reasoning, or unrelated local
data. Record the actual `gateway`, `serper`, and `web_retrieval` counts in
`run_report.json`, using zero for unused services.
