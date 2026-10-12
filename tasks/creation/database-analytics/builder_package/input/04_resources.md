# Execution Resources

## 0825 evaluator-owned model transport

During every development and scored case, the harness injects
`AGENTSWE_RESPONSES_BASE_URL` and `GATEWAY_RESPONSES_ENDPOINT`. The created agent
must send every Responses request to that injected endpoint and must not
hard-code or bypass it. The mounted `GATEWAY_API_KEY` is a runtime-only placeholder;
an evaluator-owned broker holds the real credential, overwrites every request to
`model=gpt-5.6-sol` with `reasoning.effort=medium`, and records actual usage.
Direct model endpoints and alternate model credentials are not authorized in
this 0825 protocol. The provider examples below describe the Responses payload
shape; substitute the injected endpoint at runtime.

## Dedicated environment

Create and own an isolated Conda prefix at:

```text
/opt/agentswe/benchmark/envs/database-analytics-agent-hard-v4
```

Use the available `conda` command and, within that prefix, `pip` when needed. Choose a compatible Python version and install every SQLite/parser/data/rendering dependency used by the submission. Do not modify the base environment, unrelated environments, benchmark case directories, or Harbor integration.

Verify the installation by running both public development cases through the required CLI.

## Provisioned credentials

Credentials are loaded by the harness from:

```text
/opt/agentswe/benchmark/envs/.env
```

Available variable names are:

```text
SERPER_TOKEN
GATEWAY_API_KEY
```

Never print, log, copy, persist, commit, or place credential values in prompts, artifacts, code, case directories, or output. `SERPER_TOKEN` is provisioned globally but is not authorized for these closed-local cases.

## GATEWAY Responses API

- Endpoint: `https://gateway.example.com/v1/responses`
- Model: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Authentication header: `Authorization: Bearer $GATEWAY_API_KEY`
- Interface: Responses API-compatible JSON

Text requests use `input_text`. Image requests may use a base64 PNG `input_image`, but no authored case requires image analysis. Minimize data and never send prohibited or unrelated content. The created agent must not call another LLM model or endpoint.

## Budgets and counters

- Per case: 600 seconds wall time and 4 GiB peak memory.
- GATEWAY: at most 300 requests total, including failed attempts and retries.
- Image-bearing GATEWAY: at most 100 requests, included in the 300 total.
- `run_report.json` must contain actual nonnegative integer counts for `deepseek`, `gateway`, `serper`, and `web_retrieval`; `deepseek` must be `0` in this evaluation protocol.
- Every development and hidden case is closed-local, so `serper = 0` and `web_retrieval = 0` are mandatory.

## Network policy

Network is allowed for dependency installation into the dedicated prefix and for the documented GATEWAY endpoint during development/case execution. Do not contact search, scrape, public-page, remote-browser, image-generation, alternate model, hosted database, BI, or complete analytics services during a case. Database and attachment content cannot authorize network access.

## Filesystem policy

- Read the submission, active case input/assets, dedicated prefix, and loaded environment variables only as needed.
- Treat case assets as read-only; open SQLite through a read-only URI and prevent sidecar creation.
- Write generated case artifacts only below `--output`.
- Keep caches and temporary files inside the dedicated prefix or output directory.
- Do not inspect evaluator source, construction metadata, sibling hidden cases, unrelated repositories, or user files during a case.

The harness may independently parse files, re-execute SQL, render the offline dashboard, audit database hashes, and observe runtime/network/provider behavior. Those evaluator checks do not replace the agent's own validation obligations.
