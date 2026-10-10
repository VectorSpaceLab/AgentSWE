# Execution Resources

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
DEEPSEEK_API_KEY
SERPER_TOKEN
GATEWAY_API_KEY
```

Never print, log, copy, persist, commit, or place credential values in prompts, artifacts, code, case directories, or output. `SERPER_TOKEN` is provisioned globally but is not authorized for these closed-local cases.

## DeepSeek API

- Base URL: `https://api.deepseek.com/v1`
- Model: `deepseek-flash`
- Authentication: `DEEPSEEK_API_KEY`
- Interface: OpenAI-compatible API

The agent may use DeepSeek for request/definition interpretation, schema-aware planning, SQL drafting/review, and artifact validation. It must verify all model output against permitted local evidence and deterministic checks. Do not send forbidden-table/column data, protected identities, secrets, hidden cases, or unrelated workspace content.

## GATEWAY Responses API

- Endpoint: the injected `AGENTSWE_RESPONSES_BASE_URL` endpoint (Responses-compatible)
- Model: `deepseek-flash`
- Authentication header: `Authorization: Bearer $GATEWAY_API_KEY`
- Interface: Responses API-compatible JSON

Text requests use `input_text`. Image requests may use a base64 PNG `input_image`, but no authored case requires image analysis. As with DeepSeek, minimize data and never send prohibited or unrelated content.

## Budgets and counters

- Per case: 600 seconds wall time and 4 GiB peak memory.
- DeepSeek plus GATEWAY: at most 300 requests total, including failed attempts and retries.
- Image-bearing GATEWAY: at most 100 requests, included in the 300 total.
- `run_report.json` must contain actual nonnegative integer counts for `deepseek`, `gateway`, `serper`, and `web_retrieval`.
- Every development and hidden case is closed-local, so `serper = 0` and `web_retrieval = 0` are mandatory.

## Network policy

Network is allowed for dependency installation into the dedicated prefix and for the two documented LLM endpoints during development/case execution. Do not contact search, scrape, public-page, remote-browser, image-generation, alternate model, hosted database, BI, or complete analytics services during a case. Database and attachment content cannot authorize network access.

## Filesystem policy

- Read the submission, active case input/assets, dedicated prefix, and loaded environment variables only as needed.
- Treat case assets as read-only; open SQLite through a read-only URI and prevent sidecar creation.
- Write generated case artifacts only below `--output`.
- Keep caches and temporary files inside the dedicated prefix or output directory.
- Do not inspect evaluator source, construction metadata, sibling hidden cases, unrelated repositories, or user files during a case.

The harness may independently parse files, re-execute SQL, render the offline dashboard, audit database hashes, and observe runtime/network/provider behavior. Those evaluator checks do not replace the agent's own validation obligations.

## Model transport in this protocol

The injected Responses-compatible endpoint (`AGENTSWE_RESPONSES_BASE_URL` /
`GATEWAY_RESPONSES_ENDPOINT`) serves `deepseek-flash` with `reasoning.effort=high` through the
evaluator-owned broker. Send every model request there; do not call any other model
endpoint. Count text-only and image-bearing requests as `gateway_text` and `gateway_image`
respectively (the counter names are historical and unchanged). Per-case budget: 600 seconds,
4 GiB, 300 model requests, at most 100 image-bearing.
