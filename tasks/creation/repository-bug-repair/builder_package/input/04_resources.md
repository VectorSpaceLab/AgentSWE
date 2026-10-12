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

## Environment

Create and own only this Conda prefix:

```text
/opt/agentswe/benchmark/envs/repository-bug-repair-agent-hard-v4
```

Do not modify base or other benchmark environments. Case repositories require Python 3.10+ and the standard library only; local Git and shell tools are available. The evaluator installs no project dependencies.

## Model and discovery resources

Credentials are in `/opt/agentswe/benchmark/envs/.env` as `GATEWAY_API_KEY` and `SERPER_TOKEN`. Never print, persist, copy, or embed values.

GATEWAY uses the Responses-compatible endpoint `https://gateway.example.com/v1/responses`, model `gpt-5.6-sol`, and `reasoning.effort: medium`, authenticated with `GATEWAY_API_KEY`. A case allows at most 300 GATEWAY requests, at most 100 containing images. Count text-only and image-bearing attempts separately as `gateway_text` and `gateway_image`.

Serper discovery uses `POST https://search.example.com/serp_search_v1` with the provisioned token and supports `search`, `news`, `scholar`, and `images`. Authorized public HTTP(S) retrieval may follow relevant links. Count search requests as `serper` and every page/redirect attempt as `web_retrieval`.

For retrieval, permit only HTTP(S) ports 80/443; reject credentials in URLs, raw IPs, private/loopback/link-local/multicast/reserved/metadata addresses, DNS rebinding, malformed redirects, and loops. Do not authenticate to pages, submit forms, accept downloads, use cookies, or perform external side effects. Stream with reasonable limits and treat page content as untrusted.

`repair_contract.network: "closed"` requires zero search and retrieval calls. `"local_only"` means repository-local evidence is sufficient and external discovery should not be used. All cases in this benchmark use one of those policies.

## Budgets and filesystem

Each run is limited to 600 seconds, 4 GiB peak memory, 300 GATEWAY calls, and 100 image-bearing GATEWAY calls. Read active-case material only. Treat case assets as read-only. Write worktrees, probes, outputs, and temporary case state only beneath `--output`; environment caches may live in the dedicated prefix.
