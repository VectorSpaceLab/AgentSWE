# Resources

## Local environment

- Linux, Bash, Python 3, Git, Node.js 22 or later, npm, and the package
  manager pinned in `package.json`.
- Read/write access only to the supplied repository, the public development
  cases, temporary build paths, and the delivery directory.
- Public scenarios may create Git commits, overlap local CLI processes,
  delete response files, pre-seed incomplete durable artifacts, and execute
  small local examples and search queries.
- Do not read `test_cases/`, `evaluator/`, `meta/`, earlier submissions,
  sibling benchmarks, or unrelated filesystem locations.
- Each case has 600 seconds and 4096 MiB of process-tree resident memory. No
  virtual address space limit is used.

The pinned dependency install is for installation only. Product update,
publication, and search runs are offline, with provider credentials removed
and external proxies poisoned. The manifest-driven implementation
(`--impact-manifest`) needs no registry, model, search service, browser
CDN, external lock, or live host at runtime.

## Model under the agent-loop runtime (required)

Under the agent loop, the evaluator launches the compiled production entry
as
`node <openwiki-source>/dist/cli.js --update --print --modelId <model> <case-request>`.
Cases on that path must be completed through model calls to the evaluator's
broker: the broker is supplied at runtime through the standard
OpenAI-compatible environment variables `OPENAI_BASE_URL` and
`OPENAI_API_KEY`, the model name is given by `--modelId` and repeated in
`OPENWIKI_MODEL_ID`. This is an evaluator-local relay, not external network
access, and it does not consume the GATEWAY builder quota below. A result
written without any model call scores 0 as a candidate failure; the model's
answers must be consumed into this maintenance run. See "Agent-loop result
contract" in `02_interface_and_delivery.md` for both.

## Optional builder assistance

The only authorized model service is the GATEWAY Responses API:

- `POST https://gateway.example.com/v1/responses`
- model `gpt-5.6-sol`
- reasoning effort `high`
- bearer credential environment variable `GATEWAY_API_KEY`
- at most 12 calls for the whole builder run

The only authorized search service is the configured Serper-compatible
endpoint:

- `POST https://search.example.com/serp_search_v1`
- token environment variable `SERPER_TOKEN`
- at most 8 calls for the whole builder run

At most 16 direct public result pages may be fetched and are counted as
`web_retrieval`. Optional retrieval uses the proxy `127.0.0.1:7896`. No
other models, reasoning levels, search providers, remote code analysis, or
execution services are authorized. Never transmit credentials, the
repository, assets, hidden guesses, evaluation material, or private
reasoning in bulk. Record actual counts, including zero, under
`run_report.json.api_calls`.

The deterministic manifest-driven product path needs no remote API; the
model calls required for agent-loop case execution are provided locally by
the evaluator broker and are accounted for as described under "Model under
the agent-loop runtime (required)" above.
