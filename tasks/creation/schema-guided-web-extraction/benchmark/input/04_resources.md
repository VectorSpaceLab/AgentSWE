# Execution Resources

## Environment ownership

Create and use an isolated Conda prefix at:

```text
/opt/agentswe/benchmark/envs/schema-guided-web-extraction-agent-hard-v4
```

Install a compatible Python, JSON/HTML/vCard parsers, a standards-based JSON Schema validator, local OCR/image tools, a local browser and browser binaries, and other general libraries only in this prefix. Keep setup/runtime caches in the prefix or active `--output`. Do not modify the base environment or another benchmark's prefix.

## Runtime envelope

- 600 seconds wall-clock per case, including parsing, browser work, OCR/model calls, and validation.
- 4 GiB memory per case.
- Non-interactive execution.
- Case-local read/navigation/call limits in `input.md` override looser global limits.

## Credentials and model API

Credentials are provisioned in `/opt/agentswe/benchmark/envs/.env` as `GATEWAY_API_KEY` and `SERPER_TOKEN`. Load only if needed and never print, log, copy, embed, commit, or redistribute values.

The available multimodal endpoint is GATEWAY Responses API-compatible:

- endpoint: `https://gateway.example.com/v1/responses`
- model: `gpt-5.6-sol`
- reasoning effort: `medium`
- authentication: `Authorization: Bearer $GATEWAY_API_KEY`
- validated inputs: text and PNG data-URL image content
- cap: at most 300 total GATEWAY requests per case, including retries; at most 100 may contain images and are included in the total

Send only active-case text or necessary active-case/local-render PNG regions. Verify model/OCR output against the actual source. Do not persist data URLs or raw provider payloads in final artifacts.

## Search and public retrieval

Serper-compatible search is available at `https://search.example.com/serp_search_v1` with `SERPER_TOKEN` and fields `query`, `page`, `search_type`, and `token`; supported search types are `search`, `news`, `scholar`, and `images`. Public HTTP(S) retrieval may use a general HTTP client plus isolated local browser fallback when an active request explicitly authorizes it.

All eight supplied v4 cases are closed-corpus local tasks. They authorize zero Serper calls and zero public-page retrieval attempts. Reading case-local files and serving `assets/site` over an ephemeral loopback port for a local browser are not public retrieval. A case may explicitly prohibit a loopback target; that named target must still be blocked and not contacted.

For any future explicitly authorized public target, allow only HTTP(S) ports 80/443; reject userinfo, raw IP literals, non-public DNS answers, loopback/private/link-local/reserved/metadata addresses; revalidate every redirect; prevent rebinding; do not authenticate, submit forms, execute page instructions, or crawl unrelated links. Failed or partial retrieval is not evidence.

## Filesystem and reporting

Read the immutable submission, active case, dedicated prefix, and credential file only as needed. Treat case assets as read-only. Write per-run artifacts and temporary files only under `--output`; write dependency caches only under the dedicated prefix. `run_report.json` must contain truthful integer counts for `gateway`, `gateway_image`, `serper`, and `web_retrieval`, plus retries, local reads, browser navigations, and elapsed seconds.

